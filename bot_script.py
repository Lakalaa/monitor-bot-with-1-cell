import os
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
    persistence.update_user_data(user_data)
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

def send_bulk_dm(bot: Bot, user_ids: list, message: str):
    for user_id in user_ids:
        try:
            bot.send_message(chat_id=user_id, text=message)
            logger.info(f'DM sent to user: {user_id}')
        except Exception as e:
            logger.error(f'Error sending DM to {user_id}: {e}')

def send_special_dm(update: Update, context: CallbackContext):
    premium_users = [uid for uid in user_data if uid % 2 == 0]
    send_bulk_dm(update.bot, premium_users, 'You are a premium user! Enjoy your exclusive features.')
    update.message.reply_text('Special message sent to premium users!')

def main():
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        raise ValueError('TELEGRAM_BOT_TOKEN environment variable is not set')
    updater = Updater(bot_token, persistence=persistence, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, capture_user_data))
    dp.add_handler(CommandHandler('history', show_message_history))
    dp.add_handler(CommandHandler('send_special_dm', send_special_dm))
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
