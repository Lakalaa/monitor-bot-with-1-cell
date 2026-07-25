import os, json, re, asyncio, logging, threading, time
from datetime import datetime
from collections import deque
from flask import Flask, request, abort, redirect
import requests

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_BASE       = f'https://api.telegram.org/bot{BOT_TOKEN}'
WEBHOOK_URL    = os.environ.get('WEBHOOK_URL', '')
SECRET         = os.environ.get('WEBHOOK_SECRET', 'monitorbot')
NOTIFY_CHAT_ID = os.environ.get('NOTIFY_CHAT_ID', '')
RENDER_TOKEN   = os.environ.get('RENDER_API_TOKEN', '')
SERVICE_ID     = os.environ.get('RENDER_SERVICE_ID', 'srv-d7s3ob8g4nts73d2tdsg')
TG_API_ID      = int(os.environ.get('TG_API_ID', '0'))
TG_API_HASH    = os.environ.get('TG_API_HASH', '')

# ── Storage ───────────────────────────────────────────────────────────────────
message_log   = deque(maxlen=1000)
alert_log     = deque(maxlen=300)
error_log     = deque(maxlen=100)
joined_groups: dict = {}
sessions: dict      = {}
pending_auths: dict = {}  # phone → {client, phone_code_hash, code?}
_seen_msgs    = deque(maxlen=5000)  # dedup: (chat_id, msg_id)
_admin_cache: dict = {}             # chat_id -> (frozenset of admin user_ids, fetched_ts)

# Single background event loop — all Telethon ops run here, no threading issues
_bg_loop = asyncio.new_event_loop()
threading.Thread(target=_bg_loop.run_forever, daemon=True, name='tg-auth-loop').start()

def run_async(coro, timeout=60):
    """Submit a coroutine to the background loop and block until done."""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result(timeout=timeout)

app = Flask(__name__)

# ── Category keywords ─────────────────────────────────────────────────────────
CATEGORIES = {
    'error':     ['error', 'failed', 'fail', 'stuck', 'pending', 'rejected',
                  'reverted', 'revert', 'not working', 'broken', "can't",
                  'cannot', "won't", "doesn't", 'issue', 'problem',
                  'help', '0x', 'tx hash', 'txhash', 'hash', 'infinite',
                  'lost', 'missing', 'wrong', 'invalid', 'unable'],
    'dao':       ['dao', 'governance', 'proposal', 'vote', 'voting',
                  'quorum', 'multisig', 'on-chain', 'snapshot', 'delegate'],
    'staking':   ['stake', 'staking', 'unstake', 'unstaking', 'reward',
                  'apy', 'apr', 'validator', 'delegat', 'unbond',
                  'withdraw', 'lock', 'locked', 'vesting'],
    'trading':   ['swap', 'swapping', 'trade', 'trading', 'slippage',
                  'price impact', 'liquidity', 'buy', 'sell', 'token',
                  'fill', 'order', 'position', 'long', 'short'],
    'migration': ['migrat', 'migration', 'v1', 'v2', 'v3', 'upgrade',
                  'old contract', 'new contract', 'convert', 'redeem'],
    'bridge':    ['bridge', 'bridging', 'cross-chain', 'cross chain',
                  'layer 2', 'l2', 'arbitrum', 'optimism', 'polygon',
                  'zksync', 'base', 'bsc', 'transfer'],
    'dex':       ['dex', 'uniswap', 'pancakeswap', 'sushiswap', 'curve',
                  'balancer', 'pool', ' lp ', 'liquidity pool', 'amm',
                  'router', 'pair', 'chart', 'dextools', 'dexscreener'],
}

SCAM_PATTERNS = [
    # Seed/key theft — never legitimate
    r'private\s+key', r'seed\s+phrase', r'recovery\s+phrase',
    r'secret\s+phrase', r'mnemonic', r'wallet\s+passphrase',
    # Recovery scammer phrases — targeting others, not self
    r'help\s+you\s+recover', r'i\s+can\s+help\s+you',
    r'recovery\s+(expert|specialist|service)',
    r'(recover|reclaim)\s+(your\s+)?(lost|stolen|missing)\s+(fund|token|coin|crypto)',
    r'@\w+\s+(can\s+help|helped\s+me|recovered\s+my)',
    # Guaranteed profit / money promises
    r'guaranteed\s+(profit|return|gain|roi)',
    r'double\s+your\s+(money|crypto|investment)',
    r'(make|earn)\s+\$\d+.*?(day|week|month)',
    r'\d{2,}%\s+(daily|weekly|monthly)\s+(profit|return|roi)',
    r'100%\s+(profit|return|guaranteed)',
    # Unsolicited DM pushing (scammer pattern — directing to private chat)
    r'dm\s+me\s+(now|for|to|if)',
    r'inbox\s+me\s+(now|for|to)',
    r'text\s+me\s+(now|for|to|via)',
    r'whatsapp\s+me',
    r'contact\s+(support|admin)\s+via\s+(dm|telegram|whatsapp)',
    # Fake claim / wallet drain
    r'click\s+here\s+to\s+(claim|connect|verify)',
    r'claim\s+your\s+(free|airdrop|reward|bonus)',
    r'connect\s+your\s+wallet\s+to\s+(claim|receive|get)',
    r'verify\s+your\s+wallet\s+to',
    r'free\s+(tokens?|nft|crypto)\s+(airdrop|giveaway|claim)',
]
SCAM_RE = [re.compile(p, re.I | re.S) for p in SCAM_PATTERNS]
SCAM_WORDS = {
    'seed phrase', 'private key', 'recovery phrase',
    'i can help you recover', 'i will help you recover',
    'guaranteed profit', 'guaranteed roi', 'passive income guarantee',
    'connect your wallet to claim', 'ponzi',
    # adult content
    'sex tape', 'leaked video', 'leaked content', 'onlyfans',
    'adult content', 'hot content', 'leaked nude',
}

# Bot-link spam: t.me deep links with startapp= are always bots, never user issues
import urllib.parse as _up
_TG_BOT_LINK_RE = re.compile(
    r'(https?://)?t\.me/\w+\?startapp=|'   # Telegram bot deep links
    r'(https?://)?t\.me/\w+bot',           # any @…bot username link
    re.I
)
_ADULT_EMOJI_RE = re.compile(r'[🔞💦👅🍆]')  # 🔞💦👅🍆

def is_scam(text: str) -> bool:
    t = text.lower()
    if any(w in t for w in SCAM_WORDS):
        return True
    if _TG_BOT_LINK_RE.search(text):
        return True
    if _ADULT_EMOJI_RE.search(text):
        return True
    return any(r.search(text) for r in SCAM_RE)

# ── Advertisement / marketing detector ────────────────────────────────────────
_AD_PATTERNS = [
    # P2P exchange rate ads e.g. "1 USDT = 125 INR"
    r'1\s*(usdt|btc|eth|bnb|sol)\s*=\s*[\d,.\-~]+\s*(inr|pkr|ngn|php|vnd|bdt|cny|try|rub|aed|sar|egp|idr)',
    r'(usdt|btc|eth)\s*=\s*[\d,]+\s*(rupee|inr|naira|peso|yuan)',
    r'\b(hack\s*fund|mixed\s*fund|stock\s*fund|cdm)\b',
    r'(large|high|huge|big)\s+(daily\s+)?(demand|supply|volume)\s+for\s+(usdt|btc|crypto)',
    r'complaint\s+period',
    r'(usdt|btc|eth)\s+(buyer|seller|trader|needed)',
    # Token/project promotion
    r'\$([\w]+)\s+(is|will|just|now|has)\s+(launch|moon|pump|list|explode|rally)',
    r'(just\s+launched|just\s+listed|new\s+listing|newly\s+listed)',
    r'(ido|ico|ieo|presale|pre-sale)\s+(live|open|starts?|launching|now)',
    r'(token|coin)\s+(sale|launch|drop)\s+(is\s+)?(live|open|now|today)',
    r'(nft|mint)\s+(is\s+)?(live|open|now|free|drop)',
    r'early\s+(access|investor|bird|supporter)',
    # Referral / affiliate spam
    r'(use|join\s+with|sign\s+up\s+with)\s+my\s+(ref|referral|code|link|invite)',
    r'referral\s+(code|link|bonus|reward)',
    r'invite\s+(code|link|friends)',
    r'(earn|get)\s+\d+%\s+(commission|bonus|reward)',
    # Channel / group / website promotion
    r'join\s+(our|my)\s+(channel|group|community|server)',
    r'follow\s+(our|my|us)\s+(channel|page|account)',
    r'(check\s+out|visit)\s+(our|my)\s+(website|site|channel|group)',
    r'(telegram|discord|twitter)\s*[:]\s*@?\w+\s*(for\s+more|updates|signals)',
    # Airdrop announcements
    r'(airdrop|air\s*drop)\s+(is\s+)?(live|open|now|free|going)',
    r'(free|get|earn)\s+\d+\s*(usdt|btc|eth|token)',
    # Job / service ads  
    r'(hiring|we\s+are\s+hiring|looking\s+for\s+(developers?|designers?|marketers?))',
    r'(crypto|blockchain)\s+(developer|designer|marketer)\s+(available|for\s+hire)',
    r'(professional|expert)\s+(trader|analyst|signals?)\s+(here|available)',
    # Project announcements (not issues)
    r'(we\s+are\s+)?(proud\s+to\s+)?announce',
    r'(partnership|collaboration)\s+with',
    r'(ama|ask\s+me\s+anything)\s+(session|live|today|now)',
    r'(whitepaper|roadmap|tokenomics)\s+(released|updated|published|out)',
]
_AD_RE = [re.compile(p, re.I | re.S) for p in _AD_PATTERNS]

_AD_WORDS = {
    'presale open', 'whitelist open', 'pump incoming', 'to the moon',
    'gem alert', 'buy now', 'listing soon', '100x potential',
    'financial advice', 'not financial advice', 'dyor', 'nfa',
    'signal group', 'vip signals', 'copy trading', 'managed account',
}

def is_advertisement(text: str) -> bool:
    t = text.lower()
    if any(w in t for w in _AD_WORDS):
        return True
    if any(r.search(text) for r in _AD_RE):
        return True
    # Repeated-line spam (same non-trivial line 2+ times = ad copy-paste)
    lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 10]
    if len(lines) >= 4:
        counts = {}
        for l in lines:
            counts[l] = counts.get(l, 0) + 1
        if max(counts.values()) >= 2:
            return True
    # Emoji-heavy + prices = promotional blast
    emoji_count = sum(1 for c in text if ord(c) > 0x2600)
    digits      = sum(1 for c in text if c.isdigit())
    if emoji_count >= 5 and digits >= 6 and len(lines) >= 3:
        return True
    return False

# ── Market analysis / price commentary detector ──────────────────────────────
# These are analytical posts, NOT user issues, even when they contain "I"
_MARKET_ANALYSIS_RE = re.compile(
    # Chart / TA language
    r"\b(trendline|trend\s+line|resistance\s+(zone|level|area)|support\s+(zone|level|area)"
    r"|ascending\s+trendline|descending\s+trendline"
    r"|breakout|break\s+out|rebound|bounce\s+back"
    r"|bullish|bearish|bull\s+run|bear\s+market"
    r"|moving\s+average|ema|sma|rsi|macd|bollinger"
    r"|fibonacci|fib\s+level|golden\s+cross|death\s+cross"
    r"|higher\s+high|lower\s+low|price\s+action"
    r"|overbought|oversold|consolidat"
    r"|\d+H\s+chart|\d+h\s+chart|4h|1h|daily\s+chart|weekly\s+chart"
    r"|\btf\b|timeframe|candle|wick|doji"
    r")\b"
    # Price target format: "64K", "65.5K–66K", "$64,000"
    r"|\b\d{2,3}(\.\d+)?[Kk]\s*(resistance|support|level|zone|target|area)"
    r"|\b\d{2,3}(\.\d+)?[Kk][-–]\d{2,3}(\.\d+)?[Kk]\b"
    # Prediction / expectation phrases (not personal problems)
    r"|\bi\s+expect\s+(btc|eth|sol|bnb|the\s+market|price|it)\s+to\b"
    r"|\bi\s+can\s+see\s+(btc|eth|bitcoin|price|the\s+market)\b"
    r"|\bpave\s+the\s+way\b"
    r"|\b(sellers?|buyers?)\s+(may|might|could|will)\s+become\s+(active|dominant)\b"
    r"|\b(market|price)\s+is\s+approaching\s+a\s+decisive\b",
    re.I | re.S
)

def is_market_analysis(text: str) -> bool:
    return bool(_MARKET_ANALYSIS_RE.search(text))

def categorize(text: str) -> list:
    t = text.lower()
    matched = [cat for cat, kws in CATEGORIES.items() if any(kw in t for kw in kws)]
    return matched if matched else ['general']

_FIRST_PERSON_RE = re.compile(r"\b(i|my|me|mine|i've|i'm|i'd|i'll|ive|im|we|our|us)\b", re.I)

# ── Signals strong enough to forward WITHOUT first-person ─────────────────────
_STRONG_RE = re.compile(
    # Any action that failed / is stuck
    r"\b(swap|transfer|withdraw|withdrawal|deposit|transaction|tx|buy|sell|stake"
    r"|unstake|bridge|claim|send|receive|approve|mint|redeem|convert)\s*"
    r"(fail(ed|ing)?|stuck|pending|revert(ed)?|rejected|not\s+go(ing)?|not\s+work(ing)?|not\s+complet\w*|error)\b"
    r"|\bexecution\s+reverted\b"
    r"|\bdeadline\s+exceeded\b"
    r"|\binsufficient\s+(funds?|balance|gas|liquidity)\b"
    r"|\b(gas|slippage|price\s+impact)\s+(too\s+high|error|fail)"
    # Can't do action — no first-person needed
    r"|\bcan'?t\s+(sell|buy|swap|withdraw|transfer|connect|access|stake|bridge|unstake|claim|send|receive|log\s*in|sign\s*in)\b"
    r"|\bcannot\s+(sell|buy|swap|withdraw|transfer|connect|access|stake|bridge|claim|send|receive)\b"
    r"|\bunable\s+to\s+(swap|withdraw|transfer|connect|access|stake|bridge|claim|send|receive|buy|sell)\b"
    # Community question patterns — any variant
    r"|\b(is\s+)?(anyone|someone|anybody|somebody)(\s+else|\s+here)?\s+(hav|experienc|getting|having|seeing|facing|also|too)\b"
    r"|\bhas\s+(anyone|somebody|anyone\s+else)\s+(tried|had|experienced|seen|noticed)\b"
    r"|\bsame\s+(issue|problem|thing|error|bug|situation)\b"
    r"|\bsame\s+here\b"
    # Still not resolved
    r"|\bstill\s+(no|not)\s+(received|showing|confirmed|credited|reflected|working|processed|arrived|visible|updated)\b"
    r"|\bstill\s+(pending|stuck|failing|waiting|unconfirmed)\b"
    r"|\bnot\s+yet\s+(received|showing|confirmed|credited|reflected|processed|arrived)\b"
    # Time-based complaints
    r"|\b(waited?|waiting)\s+(for\s+)?\d+\s*(hour|hr|min|day|week)"
    r"|\b\d+\s*(hour|hr|day|min)s?\s+(and|but|yet)\s+still\b"
    r"|\b(since|for\s+the\s+past|over)\s+(yesterday|last\s+\w+|\d+\s*(hour|day|week|hr|min))"
    r"|\bhow\s+long\s+(does|will|should|is)\s+(it|this|the)\b"
    r"|\bwhen\s+will\s+(it|this|my|the)\b"
    # Missing / lost funds
    r"|\b(fund|token|balance|deposit|withdrawal|money|coin|asset)s?\s+(gone|missing|disappear\w*|vanish\w*|lost|not\s+show\w*|not\s+appear\w*|not\s+reflect\w*|not\s+credit\w*|deducted)\b"
    r"|\bwhere\s+(is|are)\s+(my|the)\s+(fund|token|balance|money|deposit|withdrawal|coin)\b"
    r"|\bwhat\s+happen(ed)?\s+to\s+(my|the)\s+(fund|token|balance|money|deposit|coin)\b"
    r"|\bmoney\s+(gone|missing|deducted|lost|not\s+received)\b"
    # Not showing / not loading standalone
    r"|\b(balance|funds?|tokens?|deposit|withdrawal|transaction|tx|amount)\s+(not|isn'?t|aren'?t|doesn'?t)\s+(show\w*|appear\w*|load\w*|reflect\w*|updat\w*|credit\w*)\b"
    r"|\bpage\s+(not\s+load\w*|stuck|blank|error)\b"
    r"|\bapp\s+(not\s+work\w*|crash\w*|stuck|blank|error|down)\b"
    # Wallet issues
    r"|\bwrong\s+(network|chain|address|amount)\b"
    r"|\bwallet\s+(not|won'?t|can'?t|isn'?t)\s+(connect\w*|load\w*|work\w*|sign\w*|open\w*|link\w*)\b"
    r"|\b(metamask|phantom|trust\s*wallet|coinbase\s*wallet|walletconnect|rabby|ledger|trezor)\s+(error|issue|problem|not\s+work\w*|stuck|disconnect\w*|fail\w*)\b"
    r"|\bwallet\s+(disconnect\w*|keep\s+disconnect\w*)\b"
    # Deducted but not received
    r"|\b(deducted|charged|debited)\s+(but|and)\s+(not|never)\s+(received|credited|arrived|showing|reflected)\b"
    r"|\b(paid|sent)\s+(but|and)\s+(not|never)\s+(received|credited|arrived|showing|reflected)\b"
    # Account / access issues
    r"|\b(account|wallet|address)\s+(block\w*|restrict\w*|suspend\w*|ban\w*|frozen|locked)\b"
    r"|\bblacklist\w*\b"
    r"|\bcan'?t\s+(log\s*in|sign\s*in|access|open)\b"
    # Personal loss / scam
    r"|\b(got|been|i'?m|i\s+was)\s+(rekt|liquidated|rugged|scammed|hacked|drained|blacklisted|front.?run|sandwich\w*)\b"
    r"|\b(rug\s*pull|rugpull|rug\s*pulled|exit\s*scam)\b"
    # Why questions
    r"|\bwhy\s+(is|did|can'?t|won'?t|doesn'?t|isn'?t|hasn'?t|haven'?t)\s+(my|the|it|this)\b"
    # Asking for help / complaining
    r"|\b(please|pls|plz)\s+(help|assist|fix|check|look|respond|reply)\b"
    r"|\bneed\s+(help|support|assistance)\b"
    r"|\bno\s+(response|reply)\s+(from\s+)?(support|team|admin)\b"
    r"|\bcontacted\s+(support|team|admin)\s+(but|and|yet)\b"
    r"|\braised\s+(a\s+)?(ticket|complaint|issue)\b"
    r"|\bopen\s+(ticket|complaint)\b",
    re.I | re.S
)

# ── Problem words (used with first-person as fallback) ────────────────────────
_PROBLEM_WORDS = {
    'stuck', 'failed', 'fail', 'failing', 'keeps failing', 'keeps reverting',
    'not received', 'not arrived', 'not showing', 'not confirmed', 'not credited',
    'not reflected', 'not working', 'not processing', 'not going through',
    'not letting', 'not loading', 'not appearing', 'not completing',
    'not deposited', 'not withdrawn', 'not transferred', 'not swapped',
    "doesn't work", "didn't work", "doesn't show", "didn't receive",
    "doesn't go", "didn't go", "doesn't load", "didn't load",
    "doesn't appear", "didn't appear", "doesn't reflect", "didn't reflect",
    'wont work', 'wont go', 'wont send', 'wont load', 'wont let',
    'wont open', 'wont connect', 'wont show',
    "won't", "can't", 'cant', 'unable', 'cannot',
    'still pending', 'still waiting', 'still not', 'still no',
    'never arrived', 'never got', 'never received', 'never showed',
    'been waiting', 'waited', 'hours ago', 'days ago', 'minutes ago',
    'since yesterday', 'since last', 'since this morning', 'since last night',
    'since monday', 'since tuesday', 'since wednesday', 'since thursday',
    'since friday', 'since saturday', 'since sunday',
    'missing', 'disappeared', 'gone', 'vanished', 'not there', 'deducted',
    'wrong amount', 'wrong balance', 'short', 'less than',
    'error', 'rejected', 'reverted', 'invalid', 'execution reverted',
    'deadline exceeded', 'nonce', 'insufficient', 'gas fee',
    'failed transaction', 'failed swap', 'failed withdrawal',
    'issue', 'problem', 'trouble', 'complain', 'complaint', 'bug',
    'broken', 'down', 'offline', 'not available', 'unavailable',
    'lost', 'no response', 'no reply', 'please help', 'help me',
    'how do i fix', 'how to fix', 'fix this', 'please fix',
    'trying to', 'tried to', 'keep trying',
    'blacklisted', 'scammed', 'rugged', 'rekt', 'liquidated',
    'got hit', 'front run', 'sandwiched', 'hacked', 'drained',
    'wrong network', 'wrong chain', 'switch network',
    'why is my', 'why did my', "why can't i", 'why cant i',
    'what happened to my', 'where is my', 'where are my',
    'how long does', 'how long will', 'when will my', 'when will it',
    'is this normal', 'is this supposed', 'should it take',
}

_FINANCIAL_RE = re.compile(
    r"\b(swap|withdraw|deposit|transfer|wallet|transaction|tx|token|balance"
    r"|fund|stake|bridge|app|platform|exchange|coin|crypto)\b",
    re.I
)
_NOT_VERB_RE = re.compile(
    r"\b(not\s+work\w*|not\s+load\w*|not\s+open\w*|broken|down|offline|not\s+respond\w*)\b",
    re.I
)

def is_high_priority(cats: list, text: str) -> bool:
    t = text.lower()

    # Strong signals — forward regardless of first-person
    if _STRONG_RE.search(text):
        return True

    # First-person + any problem word
    if _FIRST_PERSON_RE.search(t) and any(p in t for p in _PROBLEM_WORDS):
        return True

    # Financial keyword + "not working/loading/broken" standalone
    if _FINANCIAL_RE.search(text) and _NOT_VERB_RE.search(text):
        return True

    return False

def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M')

# ── Telegram helpers ───────────────────────────────────────────────────────────
def tg(method, **kwargs):
    try:
        r = requests.post(f'{API_BASE}/{method}', json=kwargs, timeout=8)
        return r.json()
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        return {}

def send(chat_id, text, parse_mode='HTML'):
    for i in range(0, len(text), 4000):
        tg('sendMessage', chat_id=chat_id, text=text[i:i+4000],
           parse_mode=parse_mode, disable_web_page_preview=True)

def notify_live(entry: dict):
    if not NOTIFY_CHAT_ID:
        return
    grp     = entry['project']
    gusr    = entry.get('chat_username', '')
    grp_link = f'<a href="https://t.me/{gusr}">{grp}</a>' if gusr else f'<b>{grp}</b>'
    uname   = entry['username']
    user_str = f'@{uname}' if uname else 'Unknown'
    text    = entry['text'][:600]
    tg('sendMessage', chat_id=NOTIFY_CHAT_ID, parse_mode='HTML',
       disable_web_page_preview=True,
       text=(f'🚨 <b>USER ISSUE</b>\n'
             f'📁 {grp_link}\n'
             f'👤 <b>{user_str}</b>\n\n'
             f'💬 {text}'))

def notify_new_group(session_num, chat_title, chat_username, is_new=True):
    if not NOTIFY_CHAT_ID:
        return
    icon  = '📡' if is_new else '📂'
    label = 'New group joined' if is_new else 'Existing group'
    uname = f' (@{chat_username})' if chat_username else ''
    tg('sendMessage', chat_id=NOTIFY_CHAT_ID, parse_mode='HTML',
       text=f'{icon} <b>[#{session_num}] {label}:</b> {chat_title}{uname}')

def notify_session_start(session_num, username):
    if not NOTIFY_CHAT_ID:
        return
    tg('sendMessage', chat_id=NOTIFY_CHAT_ID, parse_mode='HTML',
       text=f'✅ <b>Session #{session_num}</b> connected as @{username}')

# ── Capture ────────────────────────────────────────────────────────────────────
def capture(msg: dict, session_num: int = 0, msg_id: int = None):
    user = msg.get('from', {})
    # Block bots and anonymous/channel senders
    if not user or user.get('is_bot') or not user.get('id'):
        return
    chat = msg.get('chat', {})
    text = msg.get('text') or msg.get('caption') or ''
    if not text.strip():
        return
    # Dedup — same (chat_id, msg_id) or same (chat_id, text) within recent window
    cid = chat.get('id')
    dedup_key = (cid, msg_id) if msg_id else (cid, hash(text[:200]))
    if dedup_key in _seen_msgs:
        return
    _seen_msgs.append(dedup_key)
    # Scam and advertisement filter
    if is_scam(text) or is_advertisement(text) or is_market_analysis(text):
        return

    cats     = categorize(text)
    uname    = user.get('username') or user.get('first_name') or str(user.get('id', '?'))
    project  = chat.get('title') or chat.get('username') or str(chat.get('id', ''))
    cid      = chat.get('id')
    chat_usr = chat.get('username', '')

    if cid and cid not in joined_groups:
        joined_groups[cid] = {
            'title': project, 'chat_id': cid,
            'chat_username': chat_usr, 'session_num': session_num,
            'joined_at': _now(), 'msg_count': 0,
        }
    if cid and cid in joined_groups:
        joined_groups[cid]['msg_count'] += 1

    priority = is_high_priority(cats, text)
    entry = {
        'time': _now(), 'user_id': user.get('id'),
        'username': uname, 'project': project,
        'chat_username': chat_usr, 'session_num': session_num,
        'text': text, 'cats': cats, 'priority': priority,
    }
    message_log.append(entry)
    if priority:
        alert_log.append(entry)
        notify_live(entry)

# ── Render helpers ─────────────────────────────────────────────────────────────
def render_get_env() -> list:
    if not RENDER_TOKEN:
        return []
    try:
        r = requests.get(
            f'https://api.render.com/v1/services/{SERVICE_ID}/env-vars',
            headers={'Authorization': f'Bearer {RENDER_TOKEN}'}, timeout=10)
        return r.json() if r.ok else []
    except Exception as e:
        logger.error('render_get_env: %s', e)
        return []

def render_put_env(env_list: list) -> bool:
    if not RENDER_TOKEN:
        return False
    try:
        r = requests.put(
            f'https://api.render.com/v1/services/{SERVICE_ID}/env-vars',
            headers={'Authorization': f'Bearer {RENDER_TOKEN}',
                     'Content-Type': 'application/json'},
            json=env_list, timeout=10)
        return r.ok
    except Exception as e:
        logger.error('render_put_env: %s', e)
        return False

def render_deploy() -> str:
    if not RENDER_TOKEN:
        return ''
    try:
        r = requests.post(
            f'https://api.render.com/v1/services/{SERVICE_ID}/deploys',
            headers={'Authorization': f'Bearer {RENDER_TOKEN}'}, timeout=10)
        return r.json().get('id', '') if r.ok else ''
    except Exception as e:
        logger.error('render_deploy: %s', e)
        return ''

def add_session_to_render(new_session: str) -> bool:
    """Append new_session to TG_SESSIONS on Render, PUT all env-vars, trigger deploy."""
    env = render_get_env()
    updated = []
    found   = False
    for e in env:
        ev  = e.get('envVar', {})
        key = ev.get('key', '')
        val = ev.get('value', '')
        if key == 'TG_SESSIONS':
            val   = f'{val},{new_session}' if val else new_session
            found = True
        updated.append({'key': key, 'value': val})
    if not found:
        updated.append({'key': 'TG_SESSIONS', 'value': new_session})
    ok = render_put_env(updated)
    if ok:
        render_deploy()
    return ok

# ── Session generator web UI ───────────────────────────────────────────────────
CSS = '''<style>
*{box-sizing:border-box}
body{font-family:monospace;background:#0d0d0d;color:#00ff88;
  max-width:520px;margin:60px auto;padding:24px}
h2{margin:0 0 16px}
p{color:#aaa;margin:8px 0}
input{width:100%;padding:12px;margin:10px 0;background:#1a1a1a;
  color:#00ff88;border:1px solid #00ff88;font-size:15px;border-radius:4px}
button{width:100%;padding:12px;margin-top:8px;background:#00ff88;
  color:#000;font-size:15px;font-weight:bold;border:none;
  border-radius:4px;cursor:pointer}
button:hover{opacity:.85}
.box{background:#1a1a1a;border:1px solid #00ff88;padding:14px;
  word-break:break-all;font-size:12px;border-radius:4px;margin:12px 0}
.err{color:#ff4444;border-color:#ff4444}
a{color:#00ff88}
.note{font-size:12px;color:#666;margin-top:6px}
</style>'''


# ── QR-code login route ────────────────────────────────────────────────────────
# State shared between routes (one pending QR session at a time)
_qr: dict = {}

def _qr_reset():
    _qr.clear()
    _qr.update(client=None, qr_url='', done=False, ok=False, msg='', started=False)

_qr_reset()

def _qr_wait_thread():
    # Runs in a daemon thread. Keeps renewing the QR until scanned or 5-min timeout.
    import time as _time
    from telethon.errors import SessionPasswordNeededError
    deadline = _time.time() + 300
    while _time.time() < deadline and not _qr['done']:
        try:
            qr_obj = run_async(_qr['client'].qr_login())
            _qr['qr_url'] = qr_obj.url
            logger.info('QR token refreshed')
            try:
                run_async(qr_obj.wait(), timeout=28)
                # Scanned!
                sess = _qr['client'].session.save()
                ok   = add_session_to_render(sess)
                _qr.update(done=True, ok=ok,
                            msg='Session added! Bot is now monitoring this account.' if ok else sess[:80])
                logger.info('QR login success; Render add=%s', ok)
                run_async(_qr['client'].disconnect())
                return
            except asyncio.TimeoutError:
                logger.info('QR wait timeout — regenerating token')
                continue
            except SessionPasswordNeededError:
                _qr.update(done=True, ok=False,
                            msg='2FA required — use /add/monitorbot instead')
                run_async(_qr['client'].disconnect())
                return
        except Exception as exc:
            _qr.update(done=True, ok=False, msg=str(exc))
            logger.exception('QR wait error: %s', exc)
            try:
                run_async(_qr['client'].disconnect())
            except Exception:
                pass
            return
    if not _qr['done']:
        _qr.update(done=True, ok=False,
                    msg='Timed out after 5 minutes. Refresh the page to try again.')
        try:
            run_async(_qr['client'].disconnect())
        except Exception:
            pass

@app.route(f'/qr/{SECRET}')
def qr_page():
    return (
        '<!DOCTYPE html><html><head><title>QR Login</title>' + CSS +
        '<meta http-equiv="refresh" content="4">'
        '</head><body>'
        '<h2>\U0001f4f1 QR Code Login \u2014 second account</h2>'
        '<p>Open Telegram on the <b>second phone/account</b>:<br>'
        '<b>Settings \u2192 Devices \u2192 Link Desktop Device</b> \u2192 scan below.</p>'
        '<p>The QR refreshes every ~28 seconds automatically.</p>'
        '<img id="qr" src="/qr/' + SECRET + '/image?t=1"'
        ' style="width:260px;height:260px;border:4px solid #00ff88;border-radius:8px"'
        ' onerror="this.style.opacity=0.3">'
        '<br><br>'
        '<div id="status" class="box" style="font-size:13px">Waiting for scan\u2026</div>'
        '<script>'
        '(function poll(){'
        'fetch("/qr/' + SECRET + '/check").then(r=>r.json()).then(d=>{'
        'if(d.done){'
        'document.getElementById("status").innerHTML='
        'd.ok?"✅ <b>"+d.msg+"</b>":"❌ "+d.msg;'
        'if(!d.ok)setTimeout(poll,5000);'
        '}else{setTimeout(poll,3000);}'
        '}).catch(()=>setTimeout(poll,5000));'
        '})();'
        'setInterval(()=>{'
        'var img=document.getElementById("qr");'
        'img.src="/qr/' + SECRET + '/image?t="+Date.now();'
        '},28000);'
        '</script>'
        '</body></html>'
    )

@app.route(f'/qr/{SECRET}/image')
def qr_image():
    import io
    from flask import Response
    import qrcode as _qrcode

    # If no session started, or previous one finished, start fresh
    if not _qr['started'] or _qr['done']:
        _qr_reset()
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        # Do NOT pass loop= (deprecated in Python 3.10+); assign it directly.
        client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
        client.loop = _bg_loop
        run_async(client.connect())
        _qr['client']  = client
        _qr['started'] = True
        threading.Thread(target=_qr_wait_thread, daemon=True, name='qr-wait').start()
        # Brief pause so the thread can populate qr_url
        import time as _t; _t.sleep(1.5)

    url = _qr.get('qr_url', '')
    if not url:
        return Response(b'', status=204)

    buf = io.BytesIO()
    _qrcode.make(url).save(buf, format='PNG')
    buf.seek(0)
    return Response(buf.getvalue(), mimetype='image/png',
                    headers={'Cache-Control': 'no-store, no-cache'})

@app.route(f'/qr/{SECRET}/check')
def qr_check():
    from flask import jsonify
    return jsonify(done=_qr.get('done', False),
                   ok=_qr.get('ok', False),
                   msg=_qr.get('msg', ''))

@app.route(f'/add/{SECRET}')
def add_page():
    api_ok = bool(TG_API_ID and TG_API_HASH)
    warn   = ('' if api_ok else
              '<p style="color:#ff4444">⚠️ TG_API_ID / TG_API_HASH not set on Render yet. '
              'Add them first then redeploy.</p>')
    return f'''<!DOCTYPE html><html><head><title>Add Phone Number</title>{CSS}</head><body>
<h2>📱 Add Phone Number</h2>
{warn}
<p>Enter the phone number you want to monitor (with country code).<br>
Telegram will send it a login code.</p>
<form action="/add/{SECRET}/start" method="post">
  <input name="phone" placeholder="+1234567890" required {"disabled" if not api_ok else ""}>
  <button type="submit" {"disabled" if not api_ok else ""}>Send Code →</button>
</form>
<p class="note">🔒 This page is private — only accessible via this secret URL.</p>
</body></html>'''

@app.route(f'/add/{SECRET}/start', methods=['POST'])
def add_start():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    phone = request.form.get('phone', '').strip()
    if not phone:
        return 'Phone required', 400
    if not TG_API_ID or not TG_API_HASH:
        return 'TG_API_ID / TG_API_HASH not set', 500

    # Clean up any existing pending auth for this phone
    if phone in pending_auths:
        try:
            old_client = pending_auths.pop(phone)['client']
            run_async(old_client.disconnect())
        except Exception:
            pass

    try:
        client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH,
                                loop=_bg_loop)
        run_async(client.connect())
        result = run_async(client.send_code_request(phone))
        pending_auths[phone] = {
            'client': client,
            'phone_code_hash': result.phone_code_hash,
        }
        logger.info('Code sent to %s', phone)
        return f'''<!DOCTYPE html><html><head><title>Enter Code</title>{CSS}</head><body>
<h2>📨 Code Sent!</h2>
<p>Telegram sent a code to <b>{phone}</b>.<br>Enter it below:</p>
<form action="/add/{SECRET}/verify" method="post">
  <input name="phone" value="{phone}" type="hidden">
  <input name="code" placeholder="e.g. 12345" required autofocus maxlength="8">
  <button type="submit">Verify & Add →</button>
</form>
<p class="note">⏱ Code expires in ~2 minutes. <a href="/add/{SECRET}">Start over</a></p>
</body></html>'''
    except Exception as e:
        logger.error('add_start error: %s', e)
        return f'''<!DOCTYPE html><html><head><title>Error</title>{CSS}</head><body>
<h2>❌ Error</h2><div class="box err">{e}</div>
<a href="/add/{SECRET}">← Try again</a></body></html>'''

@app.route(f'/add/{SECRET}/verify', methods=['POST'])
def add_verify():
    from telethon.errors import SessionPasswordNeededError

    phone = request.form.get('phone', '').strip()
    code  = request.form.get('code', '').strip()

    if phone not in pending_auths:
        return redirect(f'/add/{SECRET}')

    auth   = pending_auths[phone]
    client = auth['client']
    pch    = auth['phone_code_hash']

    try:
        run_async(client.sign_in(phone, code, phone_code_hash=pch))
        # Success — no 2FA
        session_str = client.session.save()
        run_async(client.disconnect())
        del pending_auths[phone]

        render_ok  = add_session_to_render(session_str)
        render_msg = ('✅ Added to Render automatically — redeploying now.'
                      if render_ok else
                      '⚠️ Could not update Render automatically. Copy the string below.')
        logger.info('Session saved for %s — Render: %s', phone, render_ok)
        return f'''<!DOCTYPE html><html><head><title>✅ Added</title>{CSS}</head><body>
<h2>✅ Phone Added!</h2>
<p>{render_msg}</p>
<p style="color:#aaa">Session string (keep this safe):</p>
<div class="box">{session_str}</div>
<br><a href="/add/{SECRET}">➕ Add another number</a>
</body></html>'''

    except SessionPasswordNeededError:
        # Account has 2FA — keep client alive, show password form
        logger.info('2FA required for %s', phone)
        return f'''<!DOCTYPE html><html><head><title>2FA Required</title>{CSS}</head><body>
<h2>🔐 2FA Password Required</h2>
<p>This account has two-step verification.<br>Enter your Telegram 2FA password:</p>
<form action="/add/{SECRET}/password" method="post">
  <input name="phone" value="{phone}" type="hidden">
  <input name="password" type="password" placeholder="Your 2FA password" required autofocus>
  <button type="submit">Confirm →</button>
</form>
<p class="note"><a href="/add/{SECRET}">← Start over</a></p>
</body></html>'''

    except Exception as e:
        logger.error('add_verify error: %s', e)
        return f'''<!DOCTYPE html><html><head><title>Error</title>{CSS}</head><body>
<h2>❌ Verification Failed</h2>
<div class="box err">{e}</div>
<a href="/add/{SECRET}">← Try again</a></body></html>'''

@app.route(f'/add/{SECRET}/password', methods=['POST'])
def add_password():
    phone    = request.form.get('phone', '').strip()
    password = request.form.get('password', '').strip()

    if phone not in pending_auths:
        return redirect(f'/add/{SECRET}')

    client = pending_auths[phone]['client']

    try:
        # Same client that got SessionPasswordNeededError — runs on the same _bg_loop
        run_async(client.sign_in(password=password))
        session_str = client.session.save()
        run_async(client.disconnect())
        del pending_auths[phone]

        render_ok  = add_session_to_render(session_str)
        render_msg = ('✅ Added to Render automatically — redeploying now.'
                      if render_ok else
                      '⚠️ Could not update Render automatically. Copy the string below.')
        logger.info('Session saved (2FA) for %s — Render: %s', phone, render_ok)
        return f'''<!DOCTYPE html><html><head><title>✅ Added</title>{CSS}</head><body>
<h2>✅ Phone Added!</h2>
<p>{render_msg}</p>
<p style="color:#aaa">Session string (keep this safe):</p>
<div class="box">{session_str}</div>
<br><a href="/add/{SECRET}">➕ Add another number</a>
</body></html>'''

    except Exception as e:
        logger.error('add_password error: %s', e)
        return f'''<!DOCTYPE html><html><head><title>Error</title>{CSS}</head><body>
<h2>❌ Password Failed</h2>
<div class="box err">{e}</div>
<a href="/add/{SECRET}">← Try again</a></body></html>'''

# ── Bot commands ───────────────────────────────────────────────────────────────
def register_group(chat, session_num=0, is_new=True):
    cid = chat.get('id')
    if not cid:
        return
    title  = chat.get('title') or chat.get('username') or str(cid)
    uname  = chat.get('username', '')
    if cid not in joined_groups:
        joined_groups[cid] = {
            'title': title, 'chat_id': cid, 'chat_username': uname,
            'session_num': session_num, 'joined_at': _now(), 'msg_count': 0,
        }
        notify_new_group(session_num, title, uname, is_new=is_new)

def cmd_start(cid):
    send(cid,
        '<b>🔍 Multi-Account Crypto Monitor</b>\n\n'
        'Watches every group across all added phone numbers.\n\n'
        '<b>Commands:</b>\n'
        '/alerts — ⚠️ priority alerts\n'
        '/logs — last 20 real messages\n'
        '/category [name] — error/dao/staking/trading/migration/bridge/dex\n'
        '/groups — all groups per number\n'
        '/numbers — connected phone numbers\n'
        '/users — all seen users\n'
        '/stats — summary\n'
        '/errors — runtime errors\n'
        '/clear — wipe logs')

def cmd_alerts(cid):
    items = list(alert_log)[-20:]
    if not items:
        send(cid, '✅ No high-priority alerts yet.'); return
    lines = [f'<b>⚠️ {len(items)} alert(s):</b>']
    for e in reversed(items):
        cats  = ' '.join(f'#{c}' for c in e['cats'])
        num   = e.get('session_num', '?')
        gusr  = e.get('chat_username', '')
        grp   = f"{e['project']}{' (@'+gusr+')' if gusr else ''}"
        lines.append(f"\n🕐 <code>{e['time']}</code>  {cats}\n"
                     f"<b>[#{num}]</b> 📁 {grp}\n"
                     f"👤 @{e['username']}\n💬 {e['text'][:300]}")
    send(cid, '\n'.join(lines))

def cmd_logs(cid, filter_cat=None):
    items  = [e for e in message_log if (not filter_cat or filter_cat in e['cats'])]
    recent = list(items)[-20:]
    if not recent:
        send(cid, '📭 No messages yet.'); return
    label = f' #{filter_cat}' if filter_cat else ''
    lines = [f'<b>📋 Last {len(recent)} messages{label}:</b>']
    for e in reversed(recent):
        cats = ' '.join(f'#{c}' for c in e['cats'])
        flag = ' ⚠️' if e['priority'] else ''
        num  = e.get('session_num', '?')
        gusr = e.get('chat_username', '')
        grp  = f"{e['project']}{' (@'+gusr+')' if gusr else ''}"
        lines.append(f"\n🕐 <code>{e['time']}</code>  {cats}{flag}\n"
                     f"<b>[#{num}]</b> 📁 {grp}\n"
                     f"👤 @{e['username']}\n💬 {e['text'][:300]}")
    send(cid, '\n'.join(lines))

def cmd_category(cid, text):
    parts = text.split(maxsplit=1)
    cat   = parts[1].strip().lower() if len(parts) > 1 else ''
    valid = list(CATEGORIES.keys()) + ['general']
    if cat not in valid:
        send(cid, f'Valid categories: {", ".join(valid)}'); return
    cmd_logs(cid, filter_cat=cat)

def cmd_groups(cid):
    if not joined_groups:
        send(cid, '📭 No groups tracked yet.'); return
    by_num: dict = {}
    for g in joined_groups.values():
        n = g.get('session_num', 0)
        by_num.setdefault(n, []).append(g)
    lines = [f'<b>📡 {len(joined_groups)} group(s) across {len(by_num)} number(s):</b>']
    for n in sorted(by_num):
        grps    = sorted(by_num[n], key=lambda x: -x['msg_count'])
        name    = sessions.get(n, f'Number {n}')
        lines.append(f'\n<b>#{n} @{name}</b> — {len(grps)} group(s)')
        for g in grps:
            u = f' (@{g["chat_username"]})' if g.get('chat_username') else ''
            lines.append(f"  • {g['title']}{u}  📨{g['msg_count']}")
    send(cid, '\n'.join(lines))

def cmd_numbers(cid):
    if not sessions:
        send(cid, '📭 No sessions connected yet.'); return
    lines = [f'<b>📱 {len(sessions)} connected number(s):</b>']
    for n, uname in sorted(sessions.items()):
        grp_count = sum(1 for g in joined_groups.values() if g.get('session_num') == n)
        lines.append(f'  <b>#{n}</b> @{uname} — {grp_count} group(s)')
    send(cid, '\n'.join(lines))

def cmd_users(cid):
    if not message_log:
        send(cid, '📭 No users yet.'); return
    seen: dict = {}
    for e in message_log:
        uid = e['user_id']
        if uid not in seen:
            seen[uid] = {'username': e['username'], 'count': 0,
                         'projects': set(), 'cats': set()}
        seen[uid]['count'] += 1
        seen[uid]['projects'].add(e['project'])
        seen[uid]['cats'].update(e['cats'])
    lines = [f'<b>👥 {len(seen)} unique user(s):</b>']
    for info in sorted(seen.values(), key=lambda x: -x['count']):
        cats  = ' '.join(f'#{c}' for c in info['cats'] if c != 'general')
        projs = ', '.join(info['projects'])
        lines.append(f"• @{info['username']} — {info['count']} msg(s)  {cats}\n  📁 {projs}")
    send(cid, '\n'.join(lines))

def cmd_stats(cid):
    cats_c: dict = {}
    for e in message_log:
        for c in e['cats']:
            cats_c[c] = cats_c.get(c, 0) + 1
    cat_lines = '\n'.join(f'  #{k}: {v}'
                          for k, v in sorted(cats_c.items(), key=lambda x: -x[1]))
    send(cid,
        f'<b>📊 Monitor Stats</b>\n'
        f'Sessions active: <b>{len(sessions)}</b>\n'
        f'Groups tracked: <b>{len(joined_groups)}</b>\n'
        f'Total messages: <b>{len(message_log)}</b>\n'
        f'Priority alerts: <b>{len(alert_log)}</b>\n'
        f'Unique users: <b>{len({e["user_id"] for e in message_log})}</b>\n'
        f'Runtime errors: <b>{len(error_log)}</b>\n\n'
        f'<b>By category:</b>\n{cat_lines}')

def cmd_errors_cmd(cid):
    if not error_log:
        send(cid, '✅ No runtime errors.'); return
    items = list(error_log)[-10:]
    lines = [f'<b>⚠️ Last {len(items)} error(s):</b>']
    for e in reversed(items):
        lines.append(f"\n🕐 <code>{e['time']}</code>\n❌ {e['error']}")
    send(cid, '\n'.join(lines))

def cmd_clear(cid):
    message_log.clear(); alert_log.clear(); error_log.clear()
    send(cid, '🗑 Logs cleared.')

def dispatch(msg):
    cid  = msg['chat']['id']
    text = (msg.get('text') or '').strip()
    if   text.startswith('/start'):    cmd_start(cid)
    elif text.startswith('/alerts'):   cmd_alerts(cid)
    elif text.startswith('/logs'):     cmd_logs(cid)
    elif text.startswith('/category'): cmd_category(cid, text)
    elif text.startswith('/groups'):   cmd_groups(cid)
    elif text.startswith('/numbers'):  cmd_numbers(cid)
    elif text.startswith('/users'):    cmd_users(cid)
    elif text.startswith('/stats'):    cmd_stats(cid)
    elif text.startswith('/errors'):   cmd_errors_cmd(cid)
    elif text.startswith('/clear'):    cmd_clear(cid)
    else:
        capture(msg, session_num=0)

# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route('/')
def health():
    return 'OK', 200

@app.route(f'/webhook/{SECRET}', methods=['POST'])
def webhook():
    if not request.is_json:
        abort(400)
    try:
        update = request.get_json(force=True)
        msg    = update.get('message') or update.get('channel_post')
        if msg:
            dispatch(msg)
        member = update.get('my_chat_member') or update.get('chat_member')
        if member:
            st = member.get('new_chat_member', {}).get('status', '')
            if st in ('member', 'administrator'):
                register_group(member.get('chat', {}), is_new=True)
    except Exception as exc:
        error_log.append({'time': _now(), 'error': str(exc)})
        logger.error('webhook: %s', exc, exc_info=True)
    return 'OK', 200

@app.route(f'/ingest/{SECRET}', methods=['POST'])
def ingest():
    if not request.is_json:
        abort(400)
    data  = request.get_json(force=True)
    etype = data.get('event_type', 'message')
    snum  = data.get('session_num', 1)

    if etype == 'session_start':
        uname = data.get('username', str(snum))
        sessions[snum] = uname
        # Silent — no notification
        return 'OK', 200

    if etype in ('new_group', 'existing_group'):
        cid   = data.get('chat_id')
        title = data.get('chat_title') or str(cid)
        uname = data.get('chat_username', '')
        if cid and cid not in joined_groups:
            joined_groups[cid] = {
                'title': title, 'chat_id': cid, 'chat_username': uname,
                'session_num': snum, 'joined_at': _now(), 'msg_count': 0,
            }
        # Silent — no notification for group discovery
        return 'OK', 200

    # Regular message from userbot
    cid      = data.get('chat_id')
    title    = data.get('chat_title') or str(cid)
    chat_usr = data.get('chat_username', '')
    if cid and cid not in joined_groups:
        joined_groups[cid] = {
            'title': title, 'chat_id': cid, 'chat_username': chat_usr,
            'session_num': snum, 'joined_at': _now(), 'msg_count': 0,
        }
    if cid in joined_groups:
        joined_groups[cid]['msg_count'] += 1

    capture({
        'from': {'id': data.get('user_id'), 'username': data.get('username', ''),
                 'first_name': data.get('first_name', ''), 'is_bot': False},
        'chat': {'id': cid, 'title': title, 'username': chat_usr},
        'text': data.get('text', ''),
    }, session_num=snum)
    return 'OK', 200

@app.route('/set-webhook')
def set_webhook():
    if not BOT_TOKEN or not WEBHOOK_URL:
        return 'BOT_TOKEN / WEBHOOK_URL not set', 500
    url    = f'{WEBHOOK_URL}/webhook/{SECRET}'
    result = tg('setWebhook', url=url, drop_pending_updates=True,
                allowed_updates=['message', 'channel_post', 'my_chat_member'])
    return json.dumps(result), 200

# ── Embedded userbot (runs in background thread so start.sh is not needed) ────
def _start_userbot():
    """Run the Telethon userbot in a background thread with its own event loop."""
    import asyncio
    from telethon import TelegramClient, events
    from telethon.tl.types import User, Chat, Channel
    from telethon.sessions import StringSession
    from telethon.tl.functions.account import SetPrivacyRequest
    from telethon.tl.types import (
        InputPrivacyKeyStatusTimestamp, InputPrivacyKeyProfilePhoto,
        InputPrivacyKeyPhoneNumber, InputPrivacyValueDisallowAll,
    )

    def get_ub_sessions():
        out = []
        raw = os.environ.get('TG_SESSIONS', '')
        for i, s in enumerate(raw.strip().split(','), start=1):
            s = s.strip()
            if s:
                out.append((i, s))
        return out

    async def apply_stealth(client, num):
        rules = [InputPrivacyValueDisallowAll()]
        for key in [InputPrivacyKeyStatusTimestamp(),
                    InputPrivacyKeyProfilePhoto(),
                    InputPrivacyKeyPhoneNumber()]:
            try:
                await client(SetPrivacyRequest(key=key, rules=rules))
                await asyncio.sleep(1)
            except Exception:
                pass
        logger.info('UB #%d stealth privacy applied', num)

    async def run_one(num, sess_str):
        if not TG_API_ID or not TG_API_HASH:
            logger.warning('UB #%d: TG_API_ID/HASH missing', num)
            return
        client = TelegramClient(
            StringSession(sess_str), TG_API_ID, TG_API_HASH,
            flood_sleep_threshold=60, request_retries=5,
            connection_retries=-1, retry_delay=5,
            auto_reconnect=True, receive_updates=True,
        )
        known: dict = {}

        async def scan_dialogs():
            try:
                from telethon.tl.types import ChannelParticipantsAdmins
                async for dlg in client.iter_dialogs():
                    ent = dlg.entity
                    if not isinstance(ent, (Chat, Channel)):
                        continue
                    cid   = dlg.id
                    title = getattr(ent, 'title', None) or str(cid)
                    uname = getattr(ent, 'username', None) or ''
                    if cid not in known:
                        known[cid] = title
                        joined_groups.setdefault(cid, {
                            'title': title, 'chat_id': cid,
                            'chat_username': uname, 'session_num': num,
                            'joined_at': _now(), 'msg_count': 0,
                        })
                        # Pre-warm admin cache so first message needs no API call
                        try:
                            admins = await client.get_participants(
                                ent, filter=ChannelParticipantsAdmins())
                            _admin_cache[cid] = (frozenset(u.id for u in admins), time.time())
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning('UB #%d scan error: %s', num, e)

        @client.on(events.NewMessage)
        async def on_msg(event):
            try:
                if event.out:
                    return
                chat   = await event.get_chat()
                sender = await event.get_sender()
                if not isinstance(chat, (Chat, Channel)):
                    return
                # Block bots, channel posts (sender=None), and non-user senders
                if sender is None or not isinstance(sender, User) or sender.bot:
                    return
                # Skip admins — use cached list (O(1) lookup, no API call per message)
                cid = event.chat_id
                cached = _admin_cache.get(cid)
                if cached is None or time.time() - cached[1] > 1800:
                    # Refresh admin list (once per group per 30 min)
                    try:
                        from telethon.tl.types import ChannelParticipantsAdmins
                        admins = await client.get_participants(chat, filter=ChannelParticipantsAdmins())
                        admin_ids = frozenset(u.id for u in admins)
                        _admin_cache[cid] = (admin_ids, time.time())
                    except Exception:
                        admin_ids = cached[0] if cached else frozenset()
                        _admin_cache[cid] = (admin_ids, time.time())
                else:
                    admin_ids = cached[0]
                if sender.id in admin_ids:
                    return
                text = (event.message.text or event.message.message or '').strip()
                if not text:
                    return
                cid        = event.chat_id
                msg_id     = event.message.id
                chat_title = getattr(chat, 'title', None) or str(cid)
                chat_uname = getattr(chat, 'username', None) or ''
                if cid not in known:
                    known[cid] = chat_title
                uname_s = getattr(sender, 'username', None) or ''
                first   = getattr(sender, 'first_name', None) or ''
                last    = getattr(sender, 'last_name', None) or ''
                display = uname_s or f'{first} {last}'.strip() or str(sender.id)
                capture({
                    'from': {'id': sender.id, 'username': display,
                             'first_name': first, 'is_bot': False},
                    'chat': {'id': cid, 'title': chat_title, 'username': chat_uname},
                    'text': text,
                }, session_num=num, msg_id=msg_id)
                logger.info('UB #%d [%s] @%s: %s', num, chat_title, display, text[:60])
            except Exception as e:
                logger.error('UB #%d on_msg: %s', num, e)

        logger.info('UB #%d connecting…', num)
        await client.start()
        me = await client.get_me()
        me_name = getattr(me, 'username', None) or str(me.id)
        logger.info('UB #%d connected as @%s', num, me_name)
        sessions[num] = me_name
        await apply_stealth(client, num)
        await scan_dialogs()
        logger.info('UB #%d watching', num)
        await client.run_until_disconnected()

    async def run_one_forever(num, session_str):
        """Restart session automatically on any crash — never stops."""
        backoff = 5
        while True:
            try:
                await run_one(num, session_str)
            except Exception as e:
                logger.error('UB #%d crashed: %s — restarting in %ds', num, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)  # cap at 2 min

    async def ub_main():
        ub_sessions = get_ub_sessions()
        while not ub_sessions:
            logger.info('UB: no TG_SESSIONS — retrying in 30s')
            await asyncio.sleep(30)
            ub_sessions = get_ub_sessions()
        logger.info('UB: starting %d session(s)', len(ub_sessions))
        await asyncio.gather(*[run_one_forever(n, s) for n, s in ub_sessions])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ub_main())
    except Exception as e:
        logger.error('UB thread crashed: %s', e, exc_info=True)
    finally:
        loop.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info('Monitor bot starting on 0.0.0.0:%d', port)
    # Launch embedded userbot in background thread
    ub_thread = threading.Thread(target=_start_userbot, daemon=True, name='userbot')
    ub_thread.start()
    logger.info('Userbot background thread started')
    app.run(host='0.0.0.0', port=port, use_reloader=False, threaded=True)








