import os, json, re, logging
from datetime import datetime
from collections import deque
from flask import Flask, request, abort
import requests

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_BASE    = f'https://api.telegram.org/bot{BOT_TOKEN}'
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
SECRET      = os.environ.get('WEBHOOK_SECRET', 'monitorbot')

# ── Storage (in-memory, maxlen caps RAM usage) ──────────────────────────────
message_log = deque(maxlen=1000)   # all relevant messages
alert_log   = deque(maxlen=300)    # high-priority help/error messages
error_log    = deque(maxlen=100)    # bot runtime errors
joined_groups = {}   # chat_id → {title, joined_at, msg_count}
app = Flask(__name__)

# ── Category keyword maps ───────────────────────────────────────────────────
CATEGORIES = {
    'error':      ['error', 'failed', 'fail', 'stuck', 'pending', 'rejected',
                   'reverted', 'revert', 'not working', 'broken', 'can\'t',
                   'cannot', 'won\'t', 'doesn\'t', 'issue', 'problem',
                   'help', '0x', 'tx hash', 'txhash', 'hash', 'infinite',
                   'lost', 'missing', 'wrong', 'invalid', 'unable'],
    'dao':        ['dao', 'governance', 'proposal', 'vote', 'voting',
                   'quorum', 'multisig', 'on-chain', 'snapshot', 'delegate'],
    'staking':    ['stake', 'staking', 'unstake', 'unstaking', 'reward',
                   'apy', 'apr', 'validator', 'delegat', 'unbond',
                   'withdraw', 'lock', 'locked', 'vesting'],
    'trading':    ['swap', 'swapping', 'trade', 'trading', 'slippage',
                   'price impact', 'liquidity', 'buy', 'sell', 'token',
                   'fill', 'order', 'position', 'long', 'short'],
    'migration':  ['migrat', 'migration', 'v1', 'v2', 'v3', 'upgrade',
                   'old contract', 'new contract', 'convert', 'redeem'],
    'bridge':     ['bridge', 'bridging', 'cross-chain', 'cross chain',
                   'layer 2', 'l2', 'arbitrum', 'optimism', 'polygon',
                   'zksync', 'base', 'bsc', 'transfer'],
    'dex':        ['dex', 'uniswap', 'pancakeswap', 'sushiswap', 'curve',
                   'balancer', 'pool', ' lp ', 'liquidity pool', 'amm',
                   'router', 'pair', 'chart', 'dextools', 'dexscreener'],
}

# ── Scam patterns to REJECT ─────────────────────────────────────────────────
SCAM_PATTERNS = [
    r'send\s+\d+',
    r'double\s+your',
    r'100%\s+profit',
    r'guaranteed\s+(profit|return|gain)',
    r'get\s+back\s+\d+x',
    r'private\s+key',
    r'seed\s+phrase',
    r'recovery\s+phrase',
    r'dm\s+me\s+(now|for|to)',
    r'click\s+here\s+to\s+claim',
    r'claim\s+your\s+(free|airdrop)',
    r'limited\s+time\s+offer',
    r'investment\s+plan',
    r'turn\s+.{0,20}\s+into',
    r'(make|earn)\s+\$\d+.*day',
    r'(paid|earn).*weekly',
    r'mining\s+(contract|plan)',
    r'(recover|reclaim)\s+your\s+(lost|stolen)',
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
    priority_cats = {'error', 'bridge', 'migration', 'dao'}
    if priority_cats & set(cats):
        return True
    hp_words = ['help', 'stuck', 'lost', 'failed', 'not working', 'urgent', 'asap']
    t = text.lower()
    return any(w in t for w in hp_words)

# ── Telegram helpers ─────────────────────────────────────────────────────────
def tg(method, **kwargs):
    try:
        r = requests.post(f'{API_BASE}/{method}', json=kwargs, timeout=8)
        return r.json()
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('API %s: %s', method, exc)
        return {}

def send(chat_id, text, parse_mode='HTML'):
    # Chunk at 4000 chars to stay under Telegram limit
    for i in range(0, len(text), 4000):
        tg('sendMessage', chat_id=chat_id, text=text[i:i+4000],
           parse_mode=parse_mode, disable_web_page_preview=True)

def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M')

# ── Capture logic ────────────────────────────────────────────────────────────
def capture(msg):
    user = msg.get('from', {})

    # Skip bots
    if user.get('is_bot'):
        return

    chat    = msg.get('chat', {})
    text    = msg.get('text') or msg.get('caption') or ''

    # Skip empty non-text messages (photos, stickers, etc. without captions)
    if not text.strip():
        return

    # Skip scam messages
    if is_scam(text):
        logger.info('SCAM filtered from @%s', user.get('username', '?'))
        return

    cats    = categorize(text)
    uname   = user.get('username') or user.get('first_name') or str(user.get('id', '?'))
    project  = chat.get('title') or chat.get('username') or str(chat.get('id', ''))
    chat_id_val = chat.get('id')
    uid      = user.get('id')
    # Auto-register the group if not yet tracked
    if chat_id_val and chat_id_val not in joined_groups:
        joined_groups[chat_id_val] = {
            'title': project,
            'chat_id': chat_id_val,
            'joined_at': _now(),
            'msg_count': 0,
        }
    if chat_id_val and chat_id_val in joined_groups:
        joined_groups[chat_id_val]['msg_count'] += 1
    priority = is_high_priority(cats, text)

    entry = {
        'time':     _now(),
        'user_id':  uid,
        'username': uname,
        'project':  project,
        'text':     text,
        'cats':     cats,
        'priority': priority,
    }
    message_log.append(entry)
    if priority:
        alert_log.append(entry)

    logger.info('[%s] @%s (%s)%s: %s',
                project, uname, ','.join(cats),
                ' ⚠️' if priority else '', text[:100])

# ── Commands ─────────────────────────────────────────────────────────────────
def register_group(chat):
    """Called when the bot is added to a new group/channel."""
    cid = chat.get('id')
    if not cid:
        return
    title = chat.get('title') or chat.get('username') or str(cid)
    if cid not in joined_groups:
        joined_groups[cid] = {
            'title': title,
            'chat_id': cid,
            'joined_at': _now(),
            'msg_count': 0,
        }
        logger.info('Joined new group: %s (%s)', title, cid)

def cmd_start(cid):
    send(cid,
        '<b>🔍 Crypto Support Monitor</b>\n\n'
        'I silently monitor all groups I\'m in, filter scams/bots, and log '
        'real user crypto issues.\n\n'
        '<b>Commands:</b>\n'
        '/alerts — ⚠️ high-priority help/error messages\n'
        '/logs — last 20 real messages\n'
        '/category [name] — filter by: error, dao, staking, trading, migration, bridge, dex\n'
        '/users — all users seen + message count\n'
        '/projects — all groups monitored\n'
        '/stats — summary counts\n'
        '/groups — list all groups the bot is in (with chat IDs)\n'
        '/errors — bot runtime errors\n'
        '/clear — wipe the log')

def cmd_alerts(cid):
    items = list(alert_log)[-20:]
    if not items:
        send(cid, '✅ No high-priority alerts yet.'); return
    lines = [f'<b>⚠️ {len(items)} high-priority alert(s):</b>']
    for e in reversed(items):
        cats = ' '.join(f'#{c}' for c in e['cats'])
        lines.append(
            f"\n🕐 <code>{e['time']}</code>  {cats}\n"
            f"👤 @{e['username']}  |  📁 {e['project']}\n"
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
        cats = ' '.join(f'#{c}' for c in e['cats'])
        flag = ' ⚠️' if e['priority'] else ''
        lines.append(
            f"\n🕐 <code>{e['time']}</code>  {cats}{flag}\n"
            f"👤 @{e['username']}  |  📁 {e['project']}\n"
            f"💬 {e['text'][:300]}")
    send(cid, '\n'.join(lines))

def cmd_category(cid, text):
    parts = text.split(maxsplit=1)
    cat = parts[1].strip().lower() if len(parts) > 1 else ''
    valid = list(CATEGORIES.keys()) + ['general']
    if cat not in valid:
        send(cid, f'Valid categories: {", ".join(valid)}')
        return
    cmd_logs(cid, filter_cat=cat)

def cmd_users(cid):
    if not message_log:
        send(cid, '📭 No users yet.'); return
    seen = {}
    for e in message_log:
        uid = e['user_id']
        if uid not in seen:
            seen[uid] = {'username': e['username'], 'count': 0, 'projects': set(), 'cats': set()}
        seen[uid]['count'] += 1
        seen[uid]['projects'].add(e['project'])
        seen[uid]['cats'].update(e['cats'])
    lines = [f'<b>👥 {len(seen)} unique users:</b>']
    for info in sorted(seen.values(), key=lambda x: -x['count']):
        projs = ', '.join(info['projects'])
        cats = ' '.join(f'#{c}' for c in info['cats'] if c != 'general')
        lines.append(f"• @{info['username']} — {info['count']} msg(s)  {cats}\n  📁 {projs}")
    send(cid, '\n'.join(lines))

def cmd_projects(cid):
    if not message_log:
        send(cid, '📭 No groups yet.'); return
    groups = {}
    for e in message_log:
        g = e['project']
        if g not in groups:
            groups[g] = {'count': 0, 'cats': set(), 'alerts': 0}
        groups[g]['count'] += 1
        groups[g]['cats'].update(e['cats'])
        if e['priority']:
            groups[g]['alerts'] += 1
    lines = [f'<b>📁 {len(groups)} group(s) monitored:</b>']
    for g, info in sorted(groups.items(), key=lambda x: -x[1]['count']):
        cats = ' '.join(f'#{c}' for c in info['cats'] if c != 'general')
        alert_str = f'  ⚠️{info["alerts"]}' if info['alerts'] else ''
        lines.append(f"• <b>{g}</b> — {info['count']} msg(s){alert_str}\n  {cats}")
    send(cid, '\n'.join(lines))

def cmd_stats(cid):
    total   = len(message_log)
    alerts  = len(alert_log)
    users   = len({e['user_id'] for e in message_log})
    groups  = len({e['project'] for e in message_log})
    cat_counts = {}
    for e in message_log:
        for c in e['cats']:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    cat_lines = '\n'.join(f'  #{k}: {v}' for k, v in sorted(cat_counts.items(), key=lambda x: -x[1]))
    send(cid,
        f'<b>📊 Monitor Stats:</b>\n'
        f'Total messages: <b>{total}</b>\n'
        f'High-priority alerts: <b>{alerts}</b>\n'
        f'Unique users: <b>{users}</b>\n'
        f'Groups watched: <b>{groups}</b>\n'
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
    send(cid, '🗑 All logs cleared.')

def cmd_groups(cid):
    if not joined_groups:
        send(cid, '📭 No groups found yet.\n\nThe userbot will auto-collect groups as it reads messages in them.')
        return
    lines = [f'<b>📡 {len(joined_groups)} group(s) being monitored:</b>\n']
    for info in sorted(joined_groups.values(), key=lambda x: -x['msg_count']):
        uname = f'  @{info.get("username")}' if info.get("username") else ''
        lines.append(
            f"• <b>{info['title']}</b>{uname}\n"
            f"  🆔 Chat ID: <code>{info['chat_id']}</code>\n"
            f"  📨 {info['msg_count']} msg(s)  |  📅 {info['joined_at']}"
        )
    send(cid, '\n'.join(lines))

# ── Dispatcher ────────────────────────────────────────────────────────────────
def dispatch(msg):
    chat_id = msg['chat']['id']
    text    = (msg.get('text') or '').strip()

    if text.startswith('/start'):    cmd_start(chat_id)
    elif text.startswith('/alerts'): cmd_alerts(chat_id)
    elif text.startswith('/logs'):   cmd_logs(chat_id)
    elif text.startswith('/category'): cmd_category(chat_id, text)
    elif text.startswith('/users'):  cmd_users(chat_id)
    elif text.startswith('/projects'): cmd_projects(chat_id)
    elif text.startswith('/stats'):  cmd_stats(chat_id)
    elif text.startswith('/errors'): cmd_errors_cmd(chat_id)
    elif text.startswith('/clear'):  cmd_clear(chat_id)
    elif text.startswith('/groups'): cmd_groups(chat_id)
    else:
        capture(msg)   # silently log all non-command messages

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
        msg = update.get('message') or update.get('channel_post')
        if msg:
            dispatch(msg)
        # Track when bot is added to a new group
        member_update = update.get('my_chat_member') or update.get('chat_member')
        if member_update:
            new_status = member_update.get('new_chat_member', {}).get('status', '')
            if new_status in ('member', 'administrator'):
                register_group(member_update.get('chat', {}))
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('webhook error: %s', exc, exc_info=True)
    return 'OK', 200

@app.route('/ingest/<secret>', methods=['POST'])
def ingest(secret):
    """Receive scraped message data from the Telethon userbot."""
    if secret != SECRET:
        abort(403)
    if not request.is_json:
        abort(400)
    data = request.get_json(force=True)

    # Register the group
    cid   = data.get('chat_id')
    title = data.get('chat_title') or str(cid)
    chat_user = data.get('chat_username', '')
    if cid and cid not in joined_groups:
        joined_groups[cid] = {
            'title':      title,
            'chat_id':    cid,
            'username':   chat_user,
            'joined_at':  _now(),
            'msg_count':  0,
        }
    if cid in joined_groups:
        joined_groups[cid]['msg_count'] += 1
        joined_groups[cid]['title'] = title  # keep fresh

    # Build a fake message dict so existing capture() logic works unchanged
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
            'username': chat_user,
        },
        'text': data.get('text', ''),
    }
    capture(fake_msg)
    return 'OK', 200

@app.route('/set-webhook')
def set_webhook():
    if not BOT_TOKEN:
        return 'TELEGRAM_BOT_TOKEN not set', 500
    if not WEBHOOK_URL:
        return 'WEBHOOK_URL env var not set', 500
    url = f'{WEBHOOK_URL}/webhook/{SECRET}'
    result = tg('setWebhook', url=url, drop_pending_updates=True,
                allowed_updates=['message', 'channel_post', 'my_chat_member'])
    logger.info('setWebhook → %s', result)
    return json.dumps(result), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info('Monitor bot starting on 0.0.0.0:%d', port)
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
