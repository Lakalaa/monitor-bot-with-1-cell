import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.ext import PicklePersistence
import logging

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

persistence = PicklePersistence('bot_data.pkl')
user_data = {}

def capture_user_data(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    message_text = update.message.text
    if user_id not in user_data:
        user_data[user_id] = {'username': username, 'messages': []}
    user_data[user_id]['messages'].append(message_text)
    logger.info(f'Captured message from {username}: {message_text}')
    try:
        context.bot.send_message(chat_id=user_id, text=f'Your message was captured: {message_text}')
    except Exception as e:
        logger.warning(f'Could not DM {username}: {e}')

def show_message_history(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id in user_data:
        history = '
'.join(user_data[user_id]['messages'])
        update.message.reply_text(f'Here is your message history:
{history}')
    else:
        update.message.reply_text('You have no message history with the bot yet.')

def start(update: Update, context: CallbackContext):
    update.message.reply_text('Hello! I am your monitoring bot. I will capture your messages.')

def send_bulk_dm(bot_instance: Bot, user_ids: list, message: str):
    for user_id in user_ids:
        try:
            bot_instance.send_message(chat_id=user_id, text=message)
            logger.info(f'DM sent to user: {user_id}')
        except Exception as e:
            logger.error(f'Error sending DM to {user_id}: {e}')

def send_special_dm(update: Update, context: CallbackContext):
    premium_users = [uid for uid in user_data if uid % 2 == 0]
    send_bulk_dm(update.bot, premium_users, 'You are a premium user! Enjoy your exclusive features.')
    update.message.reply_text('Special message sent to premium users!')

def run_bot():
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error('TELEGRAM_BOT_TOKEN not set — bot not started')
        return
    try:
        updater = Updater(bot_token, persistence=persistence, use_context=True)
        dp = updater.dispatcher
        dp.add_handler(CommandHandler('start', start))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, capture_user_data))
        dp.add_handler(CommandHandler('history', show_message_history))
        dp.add_handler(CommandHandler('send_special_dm', send_special_dm))
        updater.start_polling(drop_pending_updates=True)
        logger.info('Bot polling started')
        # Keep thread alive without calling updater.idle()
        # (idle() uses signal handlers which only work on the main thread)
        while updater.running:
            time.sleep(1)
    except Exception as e:
        logger.error(f'Bot thread crashed: {e}', exc_info=True)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get('PORT', 10000))
    logger.info(f'Binding health server on port {port}')
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()
