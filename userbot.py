import os, asyncio, logging, requests
from telethon import TelegramClient, events
from telethon.tl.types import User, Chat, Channel
from telethon.tl.functions.account import SetPrivacyRequest
from telethon.tl.types import (
    InputPrivacyKeyStatusTimestamp,
    InputPrivacyKeyProfilePhoto,
    InputPrivacyKeyPhoneNumber,
    InputPrivacyValueDisallowAll,
)
from telethon.sessions import StringSession

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID       = int(os.environ.get('TG_API_ID', '0'))
API_HASH     = os.environ.get('TG_API_HASH', '')
FLASK_URL    = os.environ.get('FLASK_INTERNAL_URL', 'http://127.0.0.1:10000')
FLASK_SECRET = os.environ.get('WEBHOOK_SECRET', 'monitorbot')

def get_sessions():
    sessions = []
    multi = os.environ.get('TG_SESSIONS', '')
    if multi:
        for i, s in enumerate(multi.strip().split(','), start=1):
            s = s.strip()
            if s:
                sessions.append((i, s))
    if not sessions:
        single = os.environ.get('TG_SESSION', '')
        if single.strip():
            sessions.append((1, single.strip()))
    return sessions

def push(path: str, payload: dict):
    try:
        requests.post(f'{FLASK_URL}{path}', json=payload, timeout=5)
    except Exception as e:
        logger.warning('push failed (%s): %s', path, e)

async def apply_stealth_privacy(client: TelegramClient, session_num: int):
    """
    Set all privacy options to Nobody so admins cannot see:
    - Last seen / online status
    - Profile photo
    - Phone number
    This runs once on startup per session.
    """
    rules = [InputPrivacyValueDisallowAll()]
    privacy_keys = [
        InputPrivacyKeyStatusTimestamp(),   # Last seen & online
        InputPrivacyKeyProfilePhoto(),      # Profile photo
        InputPrivacyKeyPhoneNumber(),       # Phone number
    ]
    for key in privacy_keys:
        try:
            await client(SetPrivacyRequest(key=key, rules=rules))
            await asyncio.sleep(1)          # small delay between API calls
        except Exception as e:
            logger.warning('Privacy setting failed (session #%d): %s', session_num, e)
    logger.info('Session #%d — stealth privacy applied (last seen, photo, phone → Nobody)',
                session_num)

async def run_session(session_num: int, session_str: str):
    if not API_ID or not API_HASH:
        logger.warning('TG_API_ID / TG_API_HASH not set — session #%d skipped', session_num)
        return

    client = TelegramClient(
        StringSession(session_str),
        API_ID,
        API_HASH,
        flood_sleep_threshold=60,
        request_retries=5,
        connection_retries=-1,
        retry_delay=5,
        auto_reconnect=True,
        receive_updates=True,
    )

    known_groups: dict[int, str] = {}

    async def register_dialogs():
        try:
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not isinstance(entity, (Chat, Channel)):
                    continue
                cid   = dialog.id
                title = getattr(entity, 'title', None) or str(cid)
                uname = getattr(entity, 'username', None) or ''
                if cid not in known_groups:
                    known_groups[cid] = title
                    push(f'/ingest/{FLASK_SECRET}', {
                        'event_type':    'existing_group',
                        'session_num':   session_num,
                        'chat_id':       cid,
                        'chat_title':    title,
                        'chat_username': uname,
                    })
                # Slow scan — looks human, avoids rate-limits
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning('register_dialogs error (session #%d): %s', session_num, e)

    @client.on(events.NewMessage)
    async def on_message(event):
        try:
            # Never capture messages sent by this account
            if event.out:
                return

            # Never mark as read — no read receipts ever
            # (we simply never call read_history or send_read_acknowledge)

            chat   = await event.get_chat()
            sender = await event.get_sender()

            # Groups and channels only — ignore private DMs
            if not isinstance(chat, (Chat, Channel)):
                return

            # Skip bots
            if isinstance(sender, User) and sender.bot:
                return

            text = (event.message.text or event.message.message or '').strip()
            if not text:
                return

            cid        = event.chat_id
            chat_title = getattr(chat, 'title', None) or str(cid)
            chat_uname = getattr(chat, 'username', None) or ''

            if cid not in known_groups:
                known_groups[cid] = chat_title
                push(f'/ingest/{FLASK_SECRET}', {
                    'event_type':    'new_group',
                    'session_num':   session_num,
                    'chat_id':       cid,
                    'chat_title':    chat_title,
                    'chat_username': chat_uname,
                })
                logger.info('[#%d] New group: %s', session_num, chat_title)

            user_id    = sender.id if sender else None
            username   = getattr(sender, 'username', None) or ''
            first_name = getattr(sender, 'first_name', None) or ''
            last_name  = getattr(sender, 'last_name', None) or ''
            display    = username or f'{first_name} {last_name}'.strip() or str(user_id)

            push(f'/ingest/{FLASK_SECRET}', {
                'event_type':    'message',
                'session_num':   session_num,
                'chat_id':       cid,
                'chat_title':    chat_title,
                'chat_username': chat_uname,
                'user_id':       user_id,
                'username':      display,
                'first_name':    first_name,
                'last_name':     last_name,
                'text':          text,
                'message_id':    event.message.id,
            })
            logger.info('[#%d][%s] @%s: %s', session_num, chat_title, display, text[:80])

        except Exception as e:
            logger.error('on_message error (session #%d): %s', session_num, e, exc_info=True)

    logger.info('Session #%d connecting…', session_num)
    await client.start()
    me      = await client.get_me()
    me_name = getattr(me, 'username', None) or str(getattr(me, 'id', '?'))
    logger.info('Session #%d connected as @%s', session_num, me_name)

    # Apply stealth privacy settings on every startup
    await apply_stealth_privacy(client, session_num)

    push(f'/ingest/{FLASK_SECRET}', {
        'event_type':  'session_start',
        'session_num': session_num,
        'username':    me_name,
    })

    await register_dialogs()
    logger.info('Session #%d ready — passively watching (stealth mode)', session_num)
    await client.run_until_disconnected()

async def main():
    sessions = get_sessions()
    if not sessions:
        logger.warning('No TG_SESSIONS / TG_SESSION set — userbot disabled')
        return
    logger.info('Starting %d session(s) in stealth mode…', len(sessions))
    await asyncio.gather(*[run_session(n, s) for n, s in sessions])

def start():
    asyncio.run(main())

if __name__ == '__main__':
    start()
