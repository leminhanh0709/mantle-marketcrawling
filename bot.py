import os
import logging
import feedparser
import anthropic
import requests
import schedule
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse, parse_qs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
X_BEARER_TOKEN     = os.environ["X_BEARER_TOKEN"]
RUN_ON_START       = os.environ.get("RUN_ON_START", "false").lower() == "true"

# ── COMPETITORS (X username → display name) ───────────────────────────────────
COMPETITORS = {
    "plumenetwork": "Plume",
    "arbitrum":     "Arbitrum",
    "Optimism":     "Optimism",
    "Plasma":       "Plasma",
    "BNBCHAIN":     "BNB Chain",
    "StellarOrg":   "Stellar",
    "avax":         "Avalanche",
    "CantonNetwork":"Canton Network",
    "solana":       "Solana",
    "OndoFinance":  "Ondo Finance",
}

# ── CRYPTO RSS FEEDS ──────────────────────────────────────────────────────────
CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://www.dlnews.com/arc/outboundfeeds/rss/",
]

NARRATIVES = "RWA, Infrastructure, DeFi, Institutional, Regulation, Gaming/NFT, AI, Cross-chain, Stablecoins, Tokenization"

# ── X API ─────────────────────────────────────────────────────────────────────
X_HEADERS = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}

def get_x_user_id(username: str) -> str | None:
    url = f"https://api.twitter.com/2/users/by/username/{username}"
    resp = requests.get(url, headers=X_HEADERS, timeout=10)
    if resp.ok:
        return resp.json().get("data", {}).get("id")
    logger.warning(f"X user not found: {username} — {resp.text[:100]}")
    return None

def fetch_x_tweets(username: str, user_id: str, max_results: int = 10) -> list[dict]:
    """Fetch recent tweets from a user, last 24h only."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.twitter.com/2/users/{user_id}/tweets"
    params = {
        "max_results":  max_results,
        "start_time":   since,
        "tweet.fields": "created_at,text,entities",
        "expansions":   "attachments.media_keys",
        "exclude":      "retweets,replies",
    }
    resp = requests.get(url, headers=X_HEADERS, params=params, timeout=10)
    if not resp.ok:
        logger.warning(f"X tweets failed for {username}: {resp.text[:100]}")
        return []

    data = resp.json().get("data", [])
    tweets = []
    for tweet in data:
        tweet_id = tweet["id"]
        text = tweet["text"].strip()
        link = f"https://x.com/{username}/status/{tweet_id}"
        tweets.append({"text": text[:280], "link": link})
    return tweets

def fetch_all_competitor_tweets() -> dict[str, list[dict]]:
    result = {}
    for username, display_name in COMPETITORS.items():
        user_id = get_x_user_id(username)
        if not user_id:
            continue
        tweets = fetch_x_tweets(username, user_id, max_results=10)
        if tweets:
            result[display_name] = tweets
        time.sleep(0.5)  # rate limit buffer
    logger.info(f"Fetched X tweets for {len(result)} competitors")
    return result

# ── RSS FETCH ─────────────────────────────────────────────────────────────────
def fetch_feed(url: str, max_items: int = 8) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        items = []
        for entry in feed.entries[:max_items]:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass
            if published and published < cutoff:
                continue
            items.append({
                "title": getattr(entry, "title", "").strip(),
                "link":  getattr(entry, "link", "").strip(),
            })
        return items
    except Exception as e:
        logger.warning(f"Failed {url}: {e}")
        return []

def fetch_crypto_news() -> list[dict]:
    articles = []
    for url in CRYPTO_FEEDS:
        articles.extend(fetch_feed(url, max_items=5))
    logger.info(f"Fetched {len(articles)} crypto articles")
    return articles[:30]

# ── CLAUDE ────────────────────────────────────────────────────────────────────
def call_claude(prompt: str, max_tokens: int = 600) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system="Crypto analyst. Ultra concise. No price/crash/surge/ATH news. Output only what is asked.",
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# ── PROMPTS ───────────────────────────────────────────────────────────────────
def build_competitor_prompt(competitor_tweets: dict[str, list[dict]]) -> str:
    lines = []
    for project, tweets in competitor_tweets.items():
        lines.append(f"\n### {project}")
        for t in tweets:
            lines.append(f"- {t['text']} | {t['link']}")

    return f"""These are recent tweets from competitor crypto projects:
{"".join(lines)}

For each project, pick the SINGLE most valuable tweet from last 24h.
Only include: RWA updates, trend/narrative setting, market reports, product launches, protocol upgrades, partnerships, ecosystem news.
Skip: price talk, memes, generic hype, event promos, retweets.

Output one line per project exactly:
PROJECT | NARRATIVE | Short summary (max 10 words) | link

Narratives: {NARRATIVES}
Skip projects with nothing valuable. Output lines only."""

def build_institutional_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{a['title']} | {a['link']}" for a in articles)
    return f"""Pick 3 most significant institutional moves related to RWA, tokenization, or crypto adoption (banks, funds, governments, enterprises).
No price news.

{lines}

Format (Markdown hyperlink):
• [Short title max 10 words](link)

3 bullets only."""

def build_breaking_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{a['title']} | {a['link']}" for a in articles)
    return f"""Pick 3 biggest breaking crypto events with wide market impact (hacks, regulation, major protocol events, industry shifts).
No price/token crash news.

{lines}

Format (Markdown hyperlink):
• [Short title max 10 words](link)

3 bullets only."""

# ── FORMAT COMPETITOR BLOCK ───────────────────────────────────────────────────
def format_competitor_block(raw: str) -> str:
    lines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4:
            project, narrative, title, link = parts
            lines.append(f"• *{project}* `{narrative}` — [{title}]({link})")
        elif len(parts) == 3:
            project, narrative, title = parts
            lines.append(f"• *{project}* `{narrative}` — {title}")
    return "\n".join(lines) if lines else "No significant competitor updates today."

# ── BUILD DIGEST ──────────────────────────────────────────────────────────────
def build_digest() -> str:
    logger.info("Fetching X tweets…")
    competitor_tweets = fetch_all_competitor_tweets()

    logger.info("Fetching RSS news…")
    crypto_articles = fetch_crypto_news()

    logger.info("Calling Claude…")
    raw_competitors = call_claude(build_competitor_prompt(competitor_tweets), max_tokens=800)
    institutional   = call_claude(build_institutional_prompt(crypto_articles), max_tokens=400)
    breaking        = call_claude(build_breaking_prompt(crypto_articles), max_tokens=400)

    competitor_block = format_competitor_block(raw_competitors)

    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")

    return (
        f"📰 *CRYPTO NEWS DIGEST* — {date_str}\n"
        f"{'─' * 28}\n\n"
        f"🔍 *NARRATIVES FROM COMPETITORS*\n\n"
        f"{competitor_block}\n\n"
        f"{'─' * 28}\n\n"
        f"🏦 *INSTITUTIONAL MOVES*\n\n"
        f"{institutional}\n\n"
        f"{'─' * 28}\n\n"
        f"⚡ *BREAKING & MARKET EVENTS*\n\n"
        f"{breaking}"
    )

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    ok = True
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     chunk,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        }, timeout=30)
        if not resp.ok:
            logger.error(f"Telegram error: {resp.text}")
            ok = False
    return ok

# ── JOB ───────────────────────────────────────────────────────────────────────
def run_job():
    logger.info("Running digest…")
    try:
        digest = build_digest()
        sent = send_telegram(digest)
        logger.info(f"Sent: {sent}")
    except Exception as e:
        logger.error(f"Job failed: {e}", exc_info=True)
        send_telegram(f"⚠️ Bot error: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Bot starting…")
    if RUN_ON_START:
        run_job()

    schedule.every().day.at("01:00").do(run_job)  # 01:00 UTC = 08:00 VN
    logger.info("Scheduled at 08:00 ICT")

    while True:
        schedule.run_pending()
        time.sleep(30)
