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
RUN_ON_START       = os.environ.get("RUN_ON_START", "false").lower() == "true"

# ── COMPETITORS ───────────────────────────────────────────────────────────────
COMPETITOR_PROJECTS = [
    "Solana", "Ondo Finance", "Plume", "Arbitrum", "Optimism",
    "Plasma Finance", "BNB Chain", "Stellar", "Avalanche",
]

# ── CRYPTO RSS FEEDS ──────────────────────────────────────────────────────────
CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://www.dlnews.com/arc/outboundfeeds/rss/",
]

NARRATIVES = "RWA, Infrastructure, DeFi, Institutional, Regulation, Gaming/NFT, AI, Cross-chain, Stablecoins"

# ── DECODE GOOGLE NEWS LINKS ──────────────────────────────────────────────────
def decode_google_news_url(url: str) -> str:
    """Follow Google News redirect to get the real article URL."""
    if "news.google.com" not in url:
        return url
    try:
        # Some Google News URLs have ?url= param
        qs = parse_qs(urlparse(url).query)
        if "url" in qs:
            return qs["url"][0]
        # Otherwise follow the redirect
        resp = requests.get(url, allow_redirects=True, timeout=6,
                            headers={"User-Agent": "Mozilla/5.0"})
        if "news.google.com" not in resp.url:
            return resp.url
    except Exception:
        pass
    return url

# ── FETCH ─────────────────────────────────────────────────────────────────────
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
                "link":  decode_google_news_url(getattr(entry, "link", "").strip()),
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
    return articles[:30]  # hard cap 30

def fetch_competitor_news() -> dict[str, list[dict]]:
    result = {}
    for project in COMPETITOR_PROJECTS:
        encoded = quote(f"{project} crypto")
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        items = fetch_feed(url, max_items=5)
        if items:
            result[project] = items[:3]  # max 3 per project
    logger.info(f"Fetched competitors: {len(result)} projects")
    return result

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
def build_competitor_prompt(competitor_news: dict[str, list[dict]]) -> str:
    lines = []
    for project, items in competitor_news.items():
        for item in items:
            lines.append(f"{project}: {item['title']} | {item['link']}")
    headlines = "\n".join(lines)
    return f"""Pick 1 best non-price news per project from last 24h (partnerships, launches, upgrades, RWA, regulation only).

{headlines}

Output one line per project:
PROJECT | NARRATIVE | 8-word title max | link

Narratives: {NARRATIVES}
Skip projects with no relevant news. Output lines only."""

def build_institutional_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{a['title']} | {a['link']}" for a in articles)
    return f"""Pick 3 institutional crypto moves (banks, funds, governments, enterprises). No price news.

{lines}

Format (Markdown hyperlink, keep title under 8 words):
• [Short title](link)

3 bullets only."""

def build_breaking_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{a['title']} | {a['link']}" for a in articles)
    return f"""Pick 3 biggest breaking crypto events (hacks, protocol events, regulation, industry impact). No price news.

{lines}

Format (Markdown hyperlink, keep title under 8 words):
• [Short title](link)

3 bullets only."""

# ── FORMAT ────────────────────────────────────────────────────────────────────
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
    return "\n\n".join(lines) if lines else "No significant updates today."

# ── BUILD DIGEST ──────────────────────────────────────────────────────────────
def build_digest() -> str:
    logger.info("Fetching…")
    crypto_articles = fetch_crypto_news()
    competitor_news = fetch_competitor_news()

    logger.info("Calling Claude (3 calls)…")
    raw_competitors = call_claude(build_competitor_prompt(competitor_news), max_tokens=800)
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
