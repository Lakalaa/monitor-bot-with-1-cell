import os
import json
import logging
from datetime import datetime
from collections import deque
from flask import Flask, request, abort
import requests

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_BASE  = f'https://api.telegram.org/bot{BOT_TOKEN}'
WEBHOOK_URL  = os.environ.get('WEBHOOK_URL', '')
SECRET       = os.environ.get('WEBHOOK_SECRET', 'monitorbot')

# In-memory store — resets on restart
# Each entry: {time, username, user_id, project, text}
message_log = deque(maxlen=500)   # last 500 messages across all groups
error_log   = deque(maxlen=200)   # runtime errors

app = Flask(__name__)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f'{API_BASE}/{method}', json=kwargs, timeout=10)
        return r.json()
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('API %s error: %s', method, exc)
        return {}


def send(chat_id, text, parse_mode='HTML'):
    tg('sendMessage', chat_id=chat_id, text=text, parse_mode=parse_mode,
       disable_web_page_preview=True)


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


# ── Message capture ───────────────────────────────────────────────────────────

def capture(msg):
    """Silently record every message — no reply to the sender."""
    user    = msg.get('from', {})
    chat    = msg.get('chat', {})
    uid     = user.get('id')
    uname   = user.get('username') or user.get('first_name') or str(uid)
    project = chat.get('title') or chat.get('username') or str(chat.get('id', ''))
    text    = msg.get('text') or msg.get('caption') or '[media/sticker]'

    entry = {
        'time':    _now(),
        'user_id': uid,
        'username': uname,
        'project':  project,
        'text':     text,
    }
    message_log.append(entry)
    logger.info('Captured [%s] @%s: %s', project, uname, text[:80])


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_start(chat_id):
    send(chat_id,
         '<b>Monitor Bot</b>\n\n'
         'I silently record all messages in every group I am added to.\n\n'
         '<b>Commands:</b>\n'
         '/logs — last 20 messages (username + project + text)\n'
         '/users — all users seen\n'
         '/projects — all groups being monitored\n'
         '/errors — recent errors\n'
         '/clear — clear the log\n'
         '/stats — counts summary')


def cmd_logs(chat_id):
    if not message_log:
        send(chat_id, '📭 No messages captured yet.')
        return
    recent = list(message_log)[-20:]
    lines = ['<b>📋 Last {} messages:</b>'.format(len(recent))]
    for e in reversed(recent):
        lines.append(
            f"\n🕐 <code>{e['time']}</code>\n"
            f"👤 @{e['username']}  |  📁 {e['project']}\n"
            f"💬 {e['text'][:200]}"
        )
    send(chat_id, '\n'.join(lines))


def cmd_users(chat_id):
    if not message_log:
        send(chat_id, '📭 No users captured yet.')
        return
    seen = {}
    for e in message_log:
        uid = e['user_id']
        if uid not in seen:
            seen[uid] = {'username': e['username'], 'count': 0, 'projects': set()}
        seen[uid]['count'] += 1
        seen[uid]['projects'].add(e['project'])

    lines = [f'<b>👥 {len(seen)} unique users seen:</b>']
    for uid, info in sorted(seen.items(), key=lambda x: -x[1]['count']):
        projs = ', '.join(info['projects'])
        lines.append(f"• @{info['username']} — {info['count']} msg(s) in: {projs}")
    send(chat_id, '\n'.join(lines))


def cmd_projects(chat_id):
    if not message_log:
        send(chat_id, '📭 No groups monitored yet.')
        return
    groups = {}
    for e in message_log:
        g = e['project']
        groups[g] = groups.get(g, 0) + 1
    lines = [f'<b>📁 {len(groups)} group(s) monitored:</b>']
    for g, cnt in sorted(groups.items(), key=lambda x: -x[1]):
        lines.append(f"• {g} — {cnt} message(s)")
    send(chat_id, '\n'.join(lines))


def cmd_errors(chat_id):
    if not error_log:
        send(chat_id, '✅ No errors recorded.')
        return
    recent = list(error_log)[-10:]
    lines = [f'<b>⚠️ Last {len(recent)} error(s):</b>']
    for e in reversed(recent):
        lines.append(f"\n🕐 <code>{e['time']}</code>\n❌ {e['error']}")
    send(chat_id, '\n'.join(lines))


def cmd_stats(chat_id):
    total   = len(message_log)
    users   = len({e['user_id'] for e in message_log})
    groups  = len({e['project'] for e in message_log})
    errors  = len(error_log)
    send(chat_id,
         f'<b>📊 Stats:</b>\n'
         f'Messages captured: <b>{total}</b>\n'
         f'Unique users: <b>{users}</b>\n'
         f'Groups monitored: <b>{groups}</b>\n'
         f'Errors: <b>{errors}</b>')


def cmd_clear(chat_id):
    message_log.clear()
    error_log.clear()
    send(chat_id, '🗑 Log cleared.')


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch(msg):
    chat_id = msg['chat']['id']
    text    = msg.get('text', '')

    # Commands — respond wherever the command was sent
    if text.startswith('/start'):    cmd_start(chat_id);   return
    if text.startswith('/logs'):     cmd_logs(chat_id);    return
    if text.startswith('/users'):    cmd_users(chat_id);   return
    if text.startswith('/projects'): cmd_projects(chat_id); return
    if text.startswith('/errors'):   cmd_errors(chat_id);  return
    if text.startswith('/stats'):    cmd_stats(chat_id);   return
    if text.startswith('/clear'):    cmd_clear(chat_id);   return

    # Everything else — silently capture, no reply
    capture(msg)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/')
def health():
    return 'OK', 200


@app.route(f'/webhook/{SECRET}', methods=['POST'])
def webhook():
    if not request.is_json:
        abort(400)
    try:
        update = request.get_json()
        msg = update.get('message') or update.get('channel_post')
        if msg:
            dispatch(msg)
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('webhook error: %s', exc)
    return 'OK', 200


@app.route('/set-webhook')
def set_webhook():
    if not BOT_TOKEN:
        return 'TELEGRAM_BOT_TOKEN not set', 500
    if not WEBHOOK_URL:
        return 'WEBHOOK_URL env var not set', 500
    url = f'{WEBHOOK_URL}/webhook/{SECRET}'
    result = tg('setWebhook', url=url, drop_pending_updates=True,
                allowed_updates=['message', 'channel_post'])
    logger.info('setWebhook: %s', result)
    return json.dumps(result), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info('Starting on 0.0.0.0:%d', port)
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
