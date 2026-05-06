import os, asyncio, logging, requests
from telethon import TelegramClient, events
from telethon.tl.types import User, Chat, Channel

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config from env ──────────────────────────────────────────────────────────
API_ID       = int(os.environ.get('TG_API_ID', '0'))
API_HASH     = os.environ.get('TG_API_HASH', '')
SESSION_STR  = os.environ.get('TG_SESSION', '')          # Telethon StringSession
FLASK_URL    = os.environ.get('FLASK_INTERNAL_URL', 'http://127.0.0.1:10000')
FLASK_SECRET = os.environ.get('WEBHOOK_SECRET', 'monitorbot')

def push_to_flask(payload: dict):
    """Push scraped message data to the Flask bot's internal ingest endpoint."""
    try:
        requests.post(
            f'{FLASK_URL}/ingest/{FLASK_SECRET}',
            json=payload, timeout=5
        )
    except Exception as e:
        logger.warning('push_to_flask failed: %s', e)

async def run_userbot():
    if not API_ID or not API_HASH or not SESSION_STR:
        logger.warning('TG_API_ID / TG_API_HASH / TG_SESSION not set — userbot disabled')
        return

    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            msg    = event.message
            chat   = await event.get_chat()
            sender = await event.get_sender()

            # Only care about groups / supergroups / channels
            if not isinstance(chat, (Chat, Channel)):
                return

            # Skip bots
            if isinstance(sender, User) and sender.bot:
                return

            text = msg.text or msg.message or ''
            if not text.strip():
                return

            chat_id    = event.chat_id
            chat_title = getattr(chat, 'title', None) or str(chat_id)
            chat_username = getattr(chat, 'username', None) or ''

            user_id    = sender.id if sender else None
            username   = getattr(sender, 'username', None) or ''
            first_name = getattr(sender, 'first_name', None) or ''
            last_name  = getattr(sender, 'last_name', None) or ''
            display    = username or f'{first_name} {last_name}'.strip() or str(user_id)

            payload = {
                'chat_id':       chat_id,
                'chat_title':    chat_title,
                'chat_username': chat_username,
                'user_id':       user_id,
                'username':      display,
                'first_name':    first_name,
                'last_name':     last_name,
                'text':          text,
                'message_id':    msg.id,
            }
            push_to_flask(payload)
            logger.info('[%s] @%s: %s', chat_title, display, text[:80])

        except Exception as e:
            logger.error('handler error: %s', e, exc_info=True)

    logger.info('Userbot connecting…')
    await client.start()
    logger.info('Userbot connected as: %s', await client.get_me())
    await client.run_until_disconnected()

def start():
    asyncio.run(run_userbot())

if __name__ == '__main__':
    start()
