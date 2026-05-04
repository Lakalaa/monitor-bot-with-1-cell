import os
import time
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

user_data = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, fmt, *args):
        pass


def run_bot():
    # All telegram imports are inside this thread function.
    # If the library crashes on import, the main-thread health server is unaffected.
    try:
        from telegram.ext import (
            Updater, CommandHandler, MessageHandler, Filters,
            CallbackContext, PicklePersistence,
        )
        from telegram import Update  # noqa: F401
    except Exception as exc:
        logger.error('Failed to import telegram library: %s', exc, exc_info=True)
        return

    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error('TELEGRAM_BOT_TOKEN env var not set — bot will not start')
        return

    def start(update: Update, context: CallbackContext):
        update.message.reply_text('Hello! I am your monitoring bot.')

    def capture_user_data(update: Update, context: CallbackContext):
        user_id = update.message.from_user.id
        username = update.message.from_user.username or str(user_id)
        text = update.message.text
        if user_id not in user_data:
            user_data[user_id] = {'username': username, 'messages': []}
        user_data[user_id]['messages'].append(text)
        logger.info('Captured message from %s: %s', username, text)
        try:
            context.bot.send_message(chat_id=user_id,
                                     text=f'Your message was captured: {text}')
        except Exception as exc:
            logger.warning('Could not DM %s: %s', username, exc)

    def show_message_history(update: Update, context: CallbackContext):
        user_id = update.message.from_user.id
        if user_id in user_data:
            history = '
'.join(user_data[user_id]['messages'])
            update.message.reply_text(f'Your message history:
{history}')
        else:
            update.message.reply_text('No message history yet.')

    def send_special_dm(update: Update, context: CallbackContext):
        premium_users = [uid for uid in user_data if uid % 2 == 0]
        for uid in premium_users:
            try:
                context.bot.send_message(
                    chat_id=uid,
                    text='You are a premium user! Enjoy your exclusive features.',
                )
            except Exception as exc:
                logger.error('Error sending DM to %s: %s', uid, exc)
        update.message.reply_text('Special message sent to premium users!')

    try:
        persistence = PicklePersistence('bot_data.pkl')
        updater = Updater(bot_token, persistence=persistence, use_context=True)
        dp = updater.dispatcher
        dp.add_handler(CommandHandler('start', start))
        dp.add_handler(CommandHandler('history', show_message_history))
        dp.add_handler(CommandHandler('send_special_dm', send_special_dm))
        dp.add_handler(
            MessageHandler(Filters.text & ~Filters.command, capture_user_data)
        )
        updater.start_polling(drop_pending_updates=True)
        logger.info('Bot polling started successfully')
        # Keep thread alive; do NOT call updater.idle() — it registers signal
        # handlers which are only allowed on the main thread in Python.
        while updater.running:
            time.sleep(1)
    except Exception as exc:
        logger.error('Bot crashed: %s', exc, exc_info=True)


if __name__ == '__main__':
    # Run bot in background daemon thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Bind the health-check HTTP server in the main thread so Render sees
    # PORT bound immediately — before the bot has even finished starting.
    port = int(os.environ.get('PORT', 10000))
    logger.info('Health server listening on 0.0.0.0:%d', port)
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.serve_forever()
