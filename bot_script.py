import os, json, re, logging
from datetime import datetime
from collections import deque
from flask import Flask, request, abort
import requests

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_BASE       = f'https://api.telegram.org/bot{BOT_TOKEN}'
WEBHOOK_URL    = os.environ.get('WEBHOOK_URL', '')
SECRET         = os.environ.get('WEBHOOK_SECRET', 'monitorbot')
NOTIFY_CHAT_ID = os.environ.get('NOTIFY_CHAT_ID', '')   # owner chat — live alerts go here

# ── Storage ──────────────────────────────────────────────────────────────────
message_log   = deque(maxlen=1000)
alert_log     = deque(maxlen=300)
error_log     = deque(maxlen=100)
# {chat_id: {title, chat_id, chat_username, session_num, joined_at, msg_count}}
joined_groups: dict = {}
# {session_num: username}
sessions: dict = {}

app = Flask(__name__)

# ── Category keywords ────────────────────────────────────────────────────────
CATEGORIES = {
    'error':     ['error', 'failed', 'fail', 'stuck', 'pending', 'rejected',
                  'reverted', 'revert', 'not working', 'broken', "can't",
                  'cannot', "won't", "doesn't", 'issue', 'problem',
                  'help', '0x', 'tx hash', 'txhash', 'hash', 'infinite',
                  'lost', 'missing', 'wrong', 'invalid', 'unable'],
    'dao':       ['dao', 'governance', 'proposal', 'vote', 'voting',
                  'quorum', 'multisig', 'on-chain', 'snapshot', 'delegate'],
    'staking':   ['stake', 'staking', 'unstake', 'unstaking', 'reward',
                  'apy', 'apr', 'validator', 'delegat', 'unbond',
                  'withdraw', 'lock', 'locked', 'vesting'],
    'trading':   ['swap', 'swapping', 'trade', 'trading', 'slippage',
                  'price impact', 'liquidity', 'buy', 'sell', 'token',
                  'fill', 'order', 'position', 'long', 'short'],
    'migration': ['migrat', 'migration', 'v1', 'v2', 'v3', 'upgrade',
                  'old contract', 'new contract', 'convert', 'redeem'],
    'bridge':    ['bridge', 'bridging', 'cross-chain', 'cross chain',
                  'layer 2', 'l2', 'arbitrum', 'optimism', 'polygon',
                  'zksync', 'base', 'bsc', 'transfer'],
    'dex':       ['dex', 'uniswap', 'pancakeswap', 'sushiswap', 'curve',
                  'balancer', 'pool', ' lp ', 'liquidity pool', 'amm',
                  'router', 'pair', 'chart', 'dextools', 'dexscreener'],
}

SCAM_PATTERNS = [
    r'send\s+\d+', r'double\s+your', r'100%\s+profit',
    r'guaranteed\s+(profit|return|gain)', r'get\s+back\s+\d+x',
    r'private\s+key', r'seed\s+phrase', r'recovery\s+phrase',
    r'dm\s+me\s+(now|for|to)', r'click\s+here\s+to\s+claim',
    r'claim\s+your\s+(free|airdrop)', r'limited\s+time\s+offer',
    r'investment\s+plan', r'turn\s+.{0,20}\s+into',
    r'(make|earn)\s+\$\d+.*day', r'(paid|earn).*weekly',
    r'mining\s+(contract|plan)', r'(recover|reclaim)\s+your\s+(lost|stolen)',
    r'contact\s+(support|admin)\s+via\s+(dm|telegram)',
]
SCAM_RE = [re.compile(p, re.I | re.S) for p in SCAM_PATTERNS]
SCAM_WORDS = {
    'ponzi', 'scam', 'giveaway', 'airdrop me', 'passive income guarantee',
    'roi guaranteed', 'connect your wallet to claim', 'admin dm',
}

def is_scam(text: str) -> bool:
    t = text.lower()
    if any(w in t for w in SCAM_WORDS):
        return True
    return any(r.search(text) for r in SCAM_RE)

def categorize(text: str) -> list:
    t = text.lower()
    matched = [cat for cat, kws in CATEGORIES.items() if any(kw in t for kw in kws)]
    return matched if matched else ['general']

def is_high_priority(cats: list, text: str) -> bool:
    if {'error', 'bridge', 'migration', 'dao'} & set(cats):
        return True
    hp_words = ['help', 'stuck', 'lost', 'failed', 'not working', 'urgent', 'asap']
    return any(w in text.lower() for w in hp_words)

def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M')

# ── Telegram helpers ──────────────────────────────────────────────────────────
def tg(method, **kwargs):
    try:
        r = requests.post(f'{API_BASE}/{method}', json=kwargs, timeout=8)
        return r.json()
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('API %s: %s', method, exc)
        return {}

def send(chat_id, text, parse_mode='HTML'):
    for i in range(0, len(text), 4000):
        tg('sendMessage', chat_id=chat_id, text=text[i:i+4000],
           parse_mode=parse_mode, disable_web_page_preview=True)

def notify_live(entry: dict):
    """Immediately push a priority message to the owner's chat."""
    if not NOTIFY_CHAT_ID:
        return
    cats  = ' '.join(f'#{c}' for c in entry['cats'])
    num   = entry.get('session_num', '?')
    grp   = entry['project']
    gusr  = entry.get('chat_username', '')
    grp_str = f'{grp} (@{gusr})' if gusr else grp
    user  = entry['username']
    text  = entry['text'][:500]
    msg = (
        f'⚠️ <b>[#{num}]</b>  📁 {grp_str}\n'
        f'👤 @{user}\n'
        f'💬 {text}\n'
        f'🏷 {cats}'
    )
    tg('sendMessage', chat_id=NOTIFY_CHAT_ID, text=msg,
       parse_mode='HTML', disable_web_page_preview=True)

def notify_new_group(session_num: int, chat_title: str, chat_username: str, is_new: bool = True):
    """Alert owner when a number joins a new group."""
    if not NOTIFY_CHAT_ID:
        return
    icon  = '📡' if is_new else '📂'
    label = 'New group joined' if is_new else 'Existing group'
    uname = f' (@{chat_username})' if chat_username else ''
    tg('sendMessage', chat_id=NOTIFY_CHAT_ID,
       text=f'{icon} <b>[#{session_num}] {label}:</b> {chat_title}{uname}',
       parse_mode='HTML', disable_web_page_preview=True)

def notify_session_start(session_num: int, username: str):
    if not NOTIFY_CHAT_ID:
        return
    tg('sendMessage', chat_id=NOTIFY_CHAT_ID,
       text=f'✅ <b>Session #{session_num}</b> connected as @{username}',
       parse_mode='HTML', disable_web_page_preview=True)

# ── Capture logic ─────────────────────────────────────────────────────────────
def capture(msg: dict, session_num: int = 0):
    user = msg.get('from', {})
    if user.get('is_bot'):
        return

    chat  = msg.get('chat', {})
    text  = msg.get('text') or msg.get('caption') or ''
    if not text.strip():
        return
    if is_scam(text):
        logger.info('SCAM filtered from @%s', user.get('username', '?'))
        return

    cats     = categorize(text)
    uname    = user.get('username') or user.get('first_name') or str(user.get('id', '?'))
    project  = chat.get('title') or chat.get('username') or str(chat.get('id', ''))
    cid      = chat.get('id')
    uid      = user.get('id')
    chat_usr = chat.get('username', '')

    if cid and cid not in joined_groups:
        joined_groups[cid] = {
            'title':        project,
            'chat_id':      cid,
            'chat_username': chat_usr,
            'session_num':  session_num,
            'joined_at':    _now(),
            'msg_count':    0,
        }
    if cid and cid in joined_groups:
        joined_groups[cid]['msg_count'] += 1

    priority = is_high_priority(cats, text)
    entry = {
        'time':         _now(),
        'user_id':      uid,
        'username':     uname,
        'project':      project,
        'chat_username': chat_usr,
        'session_num':  session_num,
        'text':         text,
        'cats':         cats,
        'priority':     priority,
    }
    message_log.append(entry)
    if priority:
        alert_log.append(entry)
        notify_live(entry)   # ← instant alert to owner

    logger.info('[#%d][%s] @%s (%s)%s: %s',
                session_num, project, uname, ','.join(cats),
                ' ⚠️' if priority else '', text[:100])

# ── Commands ──────────────────────────────────────────────────────────────────
def register_group(chat: dict, session_num: int = 0, is_new: bool = True):
    cid = chat.get('id')
    if not cid:
        return
    title  = chat.get('title') or chat.get('username') or str(cid)
    uname  = chat.get('username', '')
    if cid not in joined_groups:
        joined_groups[cid] = {
            'title':        title,
            'chat_id':      cid,
            'chat_username': uname,
            'session_num':  session_num,
            'joined_at':    _now(),
            'msg_count':    0,
        }
        notify_new_group(session_num, title, uname, is_new=is_new)
        logger.info('Group registered [#%d]: %s (%s)', session_num, title, cid)

def cmd_start(cid):
    send(cid,
        '<b>🔍 Multi-Account Crypto Monitor</b>\n\n'
        'Monitors every group across all added phone numbers. Filters scams and bots.\n\n'
        '<b>Commands:</b>\n'
        '/alerts — ⚠️ live priority alerts\n'
        '/logs — last 20 real messages\n'
        '/category [name] — filter: error, dao, staking, trading, migration, bridge, dex\n'
        '/groups — all groups per number\n'
        '/numbers — connected phone numbers\n'
        '/users — all seen users\n'
        '/stats — summary\n'
        '/errors — runtime errors\n'
        '/clear — wipe logs')

def cmd_alerts(cid):
    items = list(alert_log)[-20:]
    if not items:
        send(cid, '✅ No high-priority alerts yet.'); return
    lines = [f'<b>⚠️ {len(items)} alert(s):</b>']
    for e in reversed(items):
        cats  = ' '.join(f'#{c}' for c in e['cats'])
        num   = e.get('session_num', '?')
        gusr  = e.get('chat_username', '')
        grp   = f"{e['project']}{' (@' + gusr + ')' if gusr else ''}"
        lines.append(
            f"\n🕐 <code>{e['time']}</code>  {cats}\n"
            f"<b>[#{num}]</b> 📁 {grp}\n"
            f"👤 @{e['username']}\n"
            f"💬 {e['text'][:300]}")
    send(cid, '\n'.join(lines))

def cmd_logs(cid, filter_cat=None):
    items = [e for e in message_log if (not filter_cat or filter_cat in e['cats'])]
    recent = list(items)[-20:]
    if not recent:
        send(cid, '📭 No messages captured yet.'); return
    label = f' #{filter_cat}' if filter_cat else ''
    lines = [f'<b>📋 Last {len(recent)} messages{label}:</b>']
    for e in reversed(recent):
        cats  = ' '.join(f'#{c}' for c in e['cats'])
        flag  = ' ⚠️' if e['priority'] else ''
        num   = e.get('session_num', '?')
        gusr  = e.get('chat_username', '')
        grp   = f"{e['project']}{' (@' + gusr + ')' if gusr else ''}"
        lines.append(
            f"\n🕐 <code>{e['time']}</code>  {cats}{flag}\n"
            f"<b>[#{num}]</b> 📁 {grp}\n"
            f"👤 @{e['username']}\n"
            f"💬 {e['text'][:300]}")
    send(cid, '\n'.join(lines))

def cmd_category(cid, text):
    parts = text.split(maxsplit=1)
    cat   = parts[1].strip().lower() if len(parts) > 1 else ''
    valid = list(CATEGORIES.keys()) + ['general']
    if cat not in valid:
        send(cid, f'Valid categories: {", ".join(valid)}')
        return
    cmd_logs(cid, filter_cat=cat)

def cmd_groups(cid):
    if not joined_groups:
        send(cid, '📭 No groups tracked yet.'); return
    by_num: dict = {}
    for g in joined_groups.values():
        n = g.get('session_num', 0)
        by_num.setdefault(n, []).append(g)
    lines = [f'<b>📡 {len(joined_groups)} group(s) across {len(by_num)} number(s):</b>']
    for n in sorted(by_num):
        grps = sorted(by_num[n], key=lambda x: -x['msg_count'])
        num_name = sessions.get(n, f'Number {n}')
        lines.append(f'\n<b>#{n} @{num_name}</b> — {len(grps)} group(s)')
        for g in grps:
            uname = f' (@{g["chat_username"]})' if g.get('chat_username') else ''
            lines.append(f"  • {g['title']}{uname}  📨{g['msg_count']}  🆔<code>{g['chat_id']}</code>")
    send(cid, '\n'.join(lines))

def cmd_numbers(cid):
    if not sessions:
        send(cid, '📭 No sessions connected yet.'); return
    lines = [f'<b>📱 {len(sessions)} connected number(s):</b>']
    for n, uname in sorted(sessions.items()):
        grp_count = sum(1 for g in joined_groups.values() if g.get('session_num') == n)
        lines.append(f'  <b>#{n}</b> @{uname} — {grp_count} group(s)')
    send(cid, '\n'.join(lines))

def cmd_users(cid):
    if not message_log:
        send(cid, '📭 No users yet.'); return
    seen: dict = {}
    for e in message_log:
        uid = e['user_id']
        if uid not in seen:
            seen[uid] = {'username': e['username'], 'count': 0, 'projects': set(), 'cats': set()}
        seen[uid]['count'] += 1
        seen[uid]['projects'].add(e['project'])
        seen[uid]['cats'].update(e['cats'])
    lines = [f'<b>👥 {len(seen)} unique user(s):</b>']
    for info in sorted(seen.values(), key=lambda x: -x['count']):
        cats  = ' '.join(f'#{c}' for c in info['cats'] if c != 'general')
        projs = ', '.join(info['projects'])
        lines.append(f"• @{info['username']} — {info['count']} msg(s)  {cats}\n  📁 {projs}")
    send(cid, '\n'.join(lines))

def cmd_stats(cid):
    total   = len(message_log)
    alerts  = len(alert_log)
    users   = len({e['user_id'] for e in message_log})
    grps    = len(joined_groups)
    cats_c: dict = {}
    for e in message_log:
        for c in e['cats']:
            cats_c[c] = cats_c.get(c, 0) + 1
    cat_lines = '\n'.join(f'  #{k}: {v}' for k, v in sorted(cats_c.items(), key=lambda x: -x[1]))
    send(cid,
        f'<b>📊 Monitor Stats</b>\n'
        f'Sessions active: <b>{len(sessions)}</b>\n'
        f'Groups tracked: <b>{grps}</b>\n'
        f'Total messages: <b>{total}</b>\n'
        f'Priority alerts: <b>{alerts}</b>\n'
        f'Unique users: <b>{users}</b>\n'
        f'Runtime errors: <b>{len(error_log)}</b>\n\n'
        f'<b>By category:</b>\n{cat_lines}')

def cmd_errors_cmd(cid):
    if not error_log:
        send(cid, '✅ No runtime errors.'); return
    items = list(error_log)[-10:]
    lines = [f'<b>⚠️ Last {len(items)} error(s):</b>']
    for e in reversed(items):
        lines.append(f"\n🕐 <code>{e['time']}</code>\n❌ {e['error']}")
    send(cid, '\n'.join(lines))

def cmd_clear(cid):
    message_log.clear(); alert_log.clear(); error_log.clear()
    send(cid, '🗑 Logs cleared.')

# ── Dispatcher ────────────────────────────────────────────────────────────────
def dispatch(msg):
    chat_id = msg['chat']['id']
    text    = (msg.get('text') or '').strip()
    if   text.startswith('/start'):    cmd_start(chat_id)
    elif text.startswith('/alerts'):   cmd_alerts(chat_id)
    elif text.startswith('/logs'):     cmd_logs(chat_id)
    elif text.startswith('/category'): cmd_category(chat_id, text)
    elif text.startswith('/groups'):   cmd_groups(chat_id)
    elif text.startswith('/numbers'):  cmd_numbers(chat_id)
    elif text.startswith('/users'):    cmd_users(chat_id)
    elif text.startswith('/stats'):    cmd_stats(chat_id)
    elif text.startswith('/errors'):   cmd_errors_cmd(chat_id)
    elif text.startswith('/clear'):    cmd_clear(chat_id)
    else:
        capture(msg, session_num=0)

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route('/')
def health():
    return 'OK', 200

@app.route(f'/webhook/{SECRET}', methods=['POST'])
def webhook():
    if not request.is_json:
        abort(400)
    try:
        update = request.get_json(force=True)
        msg    = update.get('message') or update.get('channel_post')
        if msg:
            dispatch(msg)
        member_update = update.get('my_chat_member') or update.get('chat_member')
        if member_update:
            status = member_update.get('new_chat_member', {}).get('status', '')
            if status in ('member', 'administrator'):
                register_group(member_update.get('chat', {}), session_num=0, is_new=True)
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('webhook error: %s', exc, exc_info=True)
    return 'OK', 200

@app.route(f'/ingest/{SECRET}', methods=['POST'])
def ingest():
    if not request.is_json:
        abort(400)
    data       = request.get_json(force=True)
    event_type = data.get('event_type', 'message')
    snum       = data.get('session_num', 1)

    if event_type == 'session_start':
        uname = data.get('username', str(snum))
        sessions[snum] = uname
        notify_session_start(snum, uname)
        logger.info('Session #%d registered as @%s', snum, uname)
        return 'OK', 200

    if event_type in ('new_group', 'existing_group'):
        cid   = data.get('chat_id')
        title = data.get('chat_title') or str(cid)
        uname = data.get('chat_username', '')
        if cid and cid not in joined_groups:
            joined_groups[cid] = {
                'title':        title,
                'chat_id':      cid,
                'chat_username': uname,
                'session_num':  snum,
                'joined_at':    _now(),
                'msg_count':    0,
            }
        notify_new_group(snum, title, uname, is_new=(event_type == 'new_group'))
        return 'OK', 200

    # Regular message
    cid      = data.get('chat_id')
    title    = data.get('chat_title') or str(cid)
    chat_usr = data.get('chat_username', '')

    if cid and cid not in joined_groups:
        joined_groups[cid] = {
            'title':        title,
            'chat_id':      cid,
            'chat_username': chat_usr,
            'session_num':  snum,
            'joined_at':    _now(),
            'msg_count':    0,
        }
    if cid in joined_groups:
        joined_groups[cid]['msg_count'] += 1
        joined_groups[cid]['title'] = title

    fake_msg = {
        'from': {
            'id':         data.get('user_id'),
            'username':   data.get('username', ''),
            'first_name': data.get('first_name', ''),
            'is_bot':     False,
        },
        'chat': {
            'id':       cid,
            'title':    title,
            'username': chat_usr,
        },
        'text': data.get('text', ''),
    }
    capture(fake_msg, session_num=snum)
    return 'OK', 200

@app.route('/set-webhook')
def set_webhook():
    if not BOT_TOKEN:
        return 'TELEGRAM_BOT_TOKEN not set', 500
    if not WEBHOOK_URL:
        return 'WEBHOOK_URL env var not set', 500
    url    = f'{WEBHOOK_URL}/webhook/{SECRET}'
    result = tg('setWebhook', url=url, drop_pending_updates=True,
                allowed_updates=['message', 'channel_post', 'my_chat_member'])
    logger.info('setWebhook → %s', result)
    return json.dumps(result), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info('Monitor bot starting on 0.0.0.0:%d', port)
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
