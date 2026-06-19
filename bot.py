import os
import logging
import feedparser
import anthropic
import requests
import schedule
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

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

CRYPTO_NARRATIVES = [
    "RWA", "Infrastructure", "DeFi", "Institutional",
    "Regulation", "Gaming/NFT", "AI & Crypto",
    "Cross-chain", "Stablecoins", "Identity & Privacy",
]

# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch_feed(url: str, max_items: int = 10) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
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
                "title":   getattr(entry, "title", "").strip(),
                "summary": getattr(entry, "summary", "")[:200].strip(),
                "link":    getattr(entry, "link", "").strip(),
            })
        return items
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return []

def fetch_crypto_news() -> list[dict]:
    articles = []
    for url in CRYPTO_FEEDS:
        articles.extend(fetch_feed(url, max_items=10))
    logger.info(f"Fetched {len(articles)} crypto articles")
    return articles

def fetch_competitor_news() -> dict[str, list[dict]]:
    result = {}
    for project in COMPETITOR_PROJECTS:
        encoded = quote(f"{project} crypto")
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        items = fetch_feed(url, max_items=8)
        if items:
            result[project] = items
    logger.info(f"Fetched competitor news for {len(result)} projects")
    return result

# ── CLAUDE ────────────────────────────────────────────────────────────────────
def call_claude(prompt: str, max_tokens: int = 1500) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=(
            "You are a concise crypto analyst. "
            "Never mention price movements, crashes, surges, ATH, or liquidations. "
            "Focus on: technology, partnerships, product launches, regulation, institutional moves, RWA, DeFi. "
            "Be extremely brief. Output only what is asked, no preamble."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# ── PROMPTS ───────────────────────────────────────────────────────────────────
def build_competitor_prompt(competitor_news: dict[str, list[dict]]) -> str:
    lines = []
    for project, items in competitor_news.items():
        lines.append(f"\n### {project}")
        for item in items:
            lines.append(f"- {item['title']} | {item['link']}")

    narratives_list = ", ".join(CRYPTO_NARRATIVES)

    return f"""Recent news for each competitor:
{"".join(lines)}

For each project, pick the SINGLE most newsworthy item from the last 24h.
Only include: partnerships, launches, upgrades, institutional deals, RWA, regulation.
Skip: price news, token speculation, generic market commentary.

Output one line per project, exactly this format:
PROJECT | NARRATIVE | Short title (max 10 words) | link

Narrative must be one of: {narratives_list}
Skip any project with no meaningful news.
Output ONLY the lines, nothing else."""

def build_institutional_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"- {a['title']} | {a['link']}" for a in articles[:60])
    return f"""Headlines:
{lines}

Pick the 3 most significant institutional moves (banks, funds, governments, enterprises in crypto/blockchain).
No price news.

Format:
• Short title (max 10 words) — link

Output ONLY 3 bullets."""

def build_breaking_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"- {a['title']} | {a['link']}" for a in articles[:60])
    return f"""Headlines:
{lines}

Pick the 3 biggest breaking events (hacks, major protocol events, regulatory bombshells, industry-wide impact).
No price news, no token crashes/surges.

Format:
• Short title (max 10 words) — link

Output ONLY 3 bullets."""

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
            lines.append(f"• *{project}* — `{narrative}`\n  {title}\n  {link}")
        elif len(parts) == 3:
            project, narrative, title = parts
            lines.append(f"• *{project}* — `{narrative}`\n  {title}")
    return "\n\n".join(lines) if lines else "No significant competitor updates today."

# ── BUILD DIGEST ──────────────────────────────────────────────────────────────
def build_digest() -> str:
    logger.info("Fetching news…")
    crypto_articles = fetch_crypto_news()
    competitor_news = fetch_competitor_news()

    logger.info("Generating sections…")
    raw_competitors = call_claude(build_competitor_prompt(competitor_news))
    institutional   = call_claude(build_institutional_prompt(crypto_articles))
    breaking        = call_claude(build_breaking_prompt(crypto_articles))

    competitor_block = format_competitor_block(raw_competitors)

    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")

    message = (
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
    return message

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    success = True
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     chunk,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        }, timeout=30)
        if not resp.ok:
            logger.error(f"Telegram error: {resp.text}")
            success = False
    return success

# ── JOB ───────────────────────────────────────────────────────────────────────
def run_job():
    logger.info("Running digest job…")
    try:
        digest = build_digest()
        ok = send_telegram(digest)
        logger.info(f"Sent: {ok}")
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
