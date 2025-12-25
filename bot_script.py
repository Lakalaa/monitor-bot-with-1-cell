from telegram import Update, Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.ext import PicklePersistence

import logging

# Set up logging to help debug
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Store user data persistently
persistence = PicklePersistence("bot_data.pkl")

# Store user IDs and usernames (Scraped Data)
user_data = {}

# Function to capture user ID and username when they send a message in any group
def capture_user_data(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    username = update.message.from_user.username
    message_text = update.message.text

    if user_id not in user_data:
        user_data[user_id] = {"username": username, "messages": []}
    
    # Append the new message to the user's conversation history
    user_data[user_id]["messages"].append(message_text)

    # Save the user data persistently
    persistence.update_user_data(user_data)

    logger.info(f"Captured message from {username}: {message_text}")

    # Forward the message to the user via DM
    bot = context.bot
    bot.send_message(chat_id=user_id, text=f"Your message was captured: {message_text}")

# Function to allow the user to see their message history with the bot
def show_message_history(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id in user_data:
        message_history = "\n".join(user_data[user_id]["messages"])
        update.message.reply_text(f"Here is your message history:\n{message_history}")
    else:
        update.message.reply_text("You have no message history with the bot yet.")

# Command to start the bot and monitor messages in all groups
def start(update: Update, context: CallbackContext):
    update.message.reply_text("Hello! I am your monitoring bot. I will capture your messages.")

# Function to send a DM to all users captured so far
def send_bulk_dm(bot: Bot, user_ids: list, message: str):
    for user_id in user_ids:
        try:
            bot.send_message(chat_id=user_id, text=message)
            logger.info(f"DM sent to user: {user_id}")
        except Exception as e:
            logger.error(f"Error sending DM to {user_id}: {e}")

# Function to send a special message to premium users
def send_special_dm(update: Update, context: CallbackContext):
    # Only send a special DM to premium users (can be added by command or manual process)
    premium_users = [user_id for user_id in user_data if user_id % 2 == 0]  # Example filter
    special_message = "You are a premium user! Enjoy your exclusive features."
    send_bulk_dm(update.bot, premium_users, special_message)
    update.message.reply_text("Special message sent to premium users!")

# Main function to set up the bot
def main():
    bot_token = 'YOUR_BOT_TOKEN'  # Replace this with your bot token
    updater = Updater(bot_token, persistence=persistence, use_context=True)

    dp = updater.dispatcher

    # Handle the /start command
    dp.add_handler(CommandHandler("start", start))

    # Handle capturing messages in any group
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, capture_user_data))

    # Handle the /history command to show message history
    dp.add_handler(CommandHandler("history", show_message_history))

    # Handle the /send_special_dm command to send a special DM to premium users
    dp.add_handler(CommandHandler("send_special_dm", send_special_dm))

    # Start polling for new messages
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
