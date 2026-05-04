import os
import json
import logging
import requests
from flask import Flask, request, abort

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_BASE    = f'https://api.telegram.org/bot{BOT_TOKEN}'
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
SECRET      = os.environ.get('WEBHOOK_SECRET', 'monitorbot')
user_data   = {}

app = Flask(__name__)


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f'{API_BASE}/{method}', json=kwargs, timeout=10)
        return r.json()
    except Exception as exc:
        logger.error('API %s error: %s', method, exc)
        return {}


def send(chat_id, text):
    tg('sendMessage', chat_id=chat_id, text=text)


# ── Update handlers ───────────────────────────────────────────────────────────

def on_start(msg):
    send(msg['chat']['id'],
         'Hello! I am your monitoring bot. I will capture your messages.')


def on_history(msg):
    uid = msg['from']['id']
    if uid in user_data and user_data[uid]['messages']:
        history = '\n'.join(user_data[uid]['messages'])
        send(msg['chat']['id'], f'Your message history:\n{history}')
    else:
        send(msg['chat']['id'], 'No message history yet.')


def on_send_special_dm(msg):
    premium = [uid for uid in user_data if uid % 2 == 0]
    for uid in premium:
        try:
            send(uid, 'You are a premium user! Enjoy your exclusive features.')
        except Exception as exc:
            logger.error('DM to %s failed: %s', uid, exc)
    send(msg['chat']['id'],
         f'Special message sent to {len(premium)} premium user(s).')


def on_text(msg):
    uid  = msg['from']['id']
    name = msg['from'].get('username') or str(uid)
    text = msg.get('text', '')
    if uid not in user_data:
        user_data[uid] = {'username': name, 'messages': []}
    user_data[uid]['messages'].append(text)
    logger.info('Captured from %s: %s', name, text)
    send(uid, f'Your message was captured: {text}')


def dispatch(msg):
    text = msg.get('text', '')
    if   text.startswith('/start'):           on_start(msg)
    elif text.startswith('/history'):          on_history(msg)
    elif text.startswith('/send_special_dm'):  on_send_special_dm(msg)
    elif text:                                 on_text(msg)


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
        msg = update.get('message')
        if msg:
            dispatch(msg)
    except Exception as exc:
        logger.error('webhook error: %s', exc)
    return 'OK', 200


@app.route('/set-webhook')
def set_webhook():
    """Call this once after deployment to register the webhook with Telegram."""
    if not BOT_TOKEN:
        return 'TELEGRAM_BOT_TOKEN not set', 500
    if not WEBHOOK_URL:
        return 'WEBHOOK_URL env var not set', 500
    url = f'{WEBHOOK_URL}/webhook/{SECRET}'
    result = tg('setWebhook', url=url, drop_pending_updates=True)
    logger.info('setWebhook: %s', result)
    return json.dumps(result), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info('Starting on 0.0.0.0:%d', port)
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
