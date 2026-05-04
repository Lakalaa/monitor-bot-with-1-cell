import os
import time
import threading
import logging
import requests
from flask import Flask

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_BASE  = f'https://api.telegram.org/bot{BOT_TOKEN}'
user_data = {}

app = Flask(__name__)


@app.route('/')
def health():
    return 'OK', 200


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_post(method, **kwargs):
    try:
        r = requests.post(f'{API_BASE}/{method}', json=kwargs, timeout=10)
        return r.json()
    except Exception as exc:
        logger.error('API %s error: %s', method, exc)
        return {}


def send_message(chat_id, text):
    tg_post('sendMessage', chat_id=chat_id, text=text)


def get_updates(offset=None):
    params = {'timeout': 25, 'allowed_updates': ['message']}
    if offset is not None:
        params['offset'] = offset
    try:
        r = requests.get(f'{API_BASE}/getUpdates', params=params, timeout=35)
        return r.json().get('result', [])
    except Exception as exc:
        logger.error('getUpdates error: %s', exc)
        time.sleep(3)
        return []


# ── Handlers ──────────────────────────────────────────────────────────────────

def on_start(msg):
    send_message(msg['chat']['id'],
                 'Hello! I am your monitoring bot. I will capture your messages.')


def on_history(msg):
    uid = msg['from']['id']
    if uid in user_data and user_data[uid]['messages']:
        history = '
'.join(user_data[uid]['messages'])
        send_message(msg['chat']['id'], f'Your message history:
{history}')
    else:
        send_message(msg['chat']['id'], 'No message history yet.')


def on_send_special_dm(msg):
    premium = [uid for uid in user_data if uid % 2 == 0]
    for uid in premium:
        try:
            send_message(uid, 'You are a premium user! Enjoy your exclusive features.')
        except Exception as exc:
            logger.error('DM to %s failed: %s', uid, exc)
    send_message(msg['chat']['id'],
                 f'Special message sent to {len(premium)} premium user(s).')


def on_text(msg):
    uid  = msg['from']['id']
    name = msg['from'].get('username') or str(uid)
    text = msg.get('text', '')
    if uid not in user_data:
        user_data[uid] = {'username': name, 'messages': []}
    user_data[uid]['messages'].append(text)
    logger.info('Captured from %s: %s', name, text)
    send_message(uid, f'Your message was captured: {text}')


def dispatch(msg):
    text = msg.get('text', '')
    if   text.startswith('/start'):          on_start(msg)
    elif text.startswith('/history'):         on_history(msg)
    elif text.startswith('/send_special_dm'): on_send_special_dm(msg)
    elif text:                                on_text(msg)


# ── Polling loop ──────────────────────────────────────────────────────────────

def run_bot():
    if not BOT_TOKEN:
        logger.error('TELEGRAM_BOT_TOKEN not set — bot will not start')
        return
    logger.info('Bot polling started')
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for upd in updates:
                offset = upd['update_id'] + 1
                msg = upd.get('message')
                if msg:
                    try:
                        dispatch(msg)
                    except Exception as exc:
                        logger.error('dispatch error: %s', exc)
        except BaseException as exc:
            logger.error('polling loop crashed: %s', exc)
            time.sleep(5)


if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get('PORT', 10000))
    logger.info('Flask health server on 0.0.0.0:%d', port)
    # use_reloader=False is critical — reloader forks the process, breaking daemon threads
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)
