import os
import time
import threading
import logging
import json
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API = f'https://api.telegram.org/bot{BOT_TOKEN}'
user_data = {}   # {user_id: {'username': str, 'messages': [str]}}


# ── Telegram API helpers ────────────────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f'{API}/{method}', json=kwargs, timeout=10)
        return r.json()
    except Exception as exc:
        logger.error('Telegram API error (%s): %s', method, exc)
        return {}


def send(chat_id, text):
    tg('sendMessage', chat_id=chat_id, text=text)


def get_updates(offset=None):
    params = {'timeout': 20, 'allowed_updates': ['message']}
    if offset is not None:
        params['offset'] = offset
    try:
        r = requests.get(f'{API}/getUpdates', params=params, timeout=30)
        return r.json().get('result', [])
    except Exception as exc:
        logger.error('getUpdates error: %s', exc)
        return []


# ── Command / message handlers ──────────────────────────────────────────────

def handle_start(msg):
    send(msg['chat']['id'], 'Hello! I am your monitoring bot. I will capture your messages.')


def handle_history(msg):
    user_id = msg['from']['id']
    if user_id in user_data and user_data[user_id]['messages']:
        history = '
'.join(user_data[user_id]['messages'])
        send(msg['chat']['id'], f'Your message history:
{history}')
    else:
        send(msg['chat']['id'], 'No message history yet.')


def handle_send_special_dm(msg):
    premium_users = [uid for uid in user_data if uid % 2 == 0]
    for uid in premium_users:
        send(uid, 'You are a premium user! Enjoy your exclusive features.')
    send(msg['chat']['id'], f'Special message sent to {len(premium_users)} premium user(s).')


def handle_message(msg):
    user_id = msg['from']['id']
    username = msg['from'].get('username') or str(user_id)
    text = msg.get('text', '')
    if user_id not in user_data:
        user_data[user_id] = {'username': username, 'messages': []}
    user_data[user_id]['messages'].append(text)
    logger.info('Captured from %s: %s', username, text)
    send(user_id, f'Your message was captured: {text}')


def dispatch(msg):
    text = msg.get('text', '')
    if text.startswith('/start'):
        handle_start(msg)
    elif text.startswith('/history'):
        handle_history(msg)
    elif text.startswith('/send_special_dm'):
        handle_send_special_dm(msg)
    elif text:
        handle_message(msg)


# ── Polling loop ─────────────────────────────────────────────────────────────

def run_bot():
    if not BOT_TOKEN:
        logger.error('TELEGRAM_BOT_TOKEN not set — bot will not start')
        return
    logger.info('Bot polling started')
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update['update_id'] + 1
                msg = update.get('message')
                if msg:
                    dispatch(msg)
        except Exception as exc:
            logger.error('Polling loop error: %s', exc)
            time.sleep(5)


# ── Health-check HTTP server (main thread) ───────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get('PORT', 10000))
    logger.info('Health server on 0.0.0.0:%d', port)
    HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()
