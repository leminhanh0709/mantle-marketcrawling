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

# ── RSS FEEDS ─────────────────────────────────────────────────────────────────
CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://www.dlnews.com/arc/outboundfeeds/rss/",
]

# Google News RSS for competitor watch
COMPETITOR_PROJECTS = [
    "Solana", "Ondo Finance", "Plume", "Arbitrum", "Optimism",
    "Plasma Finance", "BNB Chain", "Stellar", "Avalanche",
]

def google_news_rss(query: str) -> str:
    encoded = quote(f"{query} crypto")
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

# ── FETCH ARTICLES ────────────────────────────────────────────────────────────
def fetch_feed(url: str, max_items: int = 10) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            items.append({
                "title":   getattr(entry, "title", ""),
                "summary": getattr(entry, "summary", "")[:300],
                "link":    getattr(entry, "link", ""),
            })
        return items
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return []

def fetch_all_crypto_news() -> list[dict]:
    articles = []
    for url in CRYPTO_FEEDS:
        articles.extend(fetch_feed(url, max_items=8))
    logger.info(f"Fetched {len(articles)} crypto articles")
    return articles

def fetch_competitor_news() -> dict[str, list[dict]]:
    result = {}
    for project in COMPETITOR_PROJECTS:
        url = google_news_rss(project)
        items = fetch_feed(url, max_items=6)
        if items:
            result[project] = items
    logger.info(f"Fetched competitor news for {len(result)} projects")
    return result

# ── SUMMARISE WITH CLAUDE ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional crypto analyst who writes concise, insight-driven daily digests.
Rules:
- NO price action: ignore crash/surge/ATH/liquidation/price prediction news entirely
- Focus on: technology, partnerships, product launches, regulation, institutional adoption, RWA, DeFi, tokenization
- Each section: max 2-3 bullet points
- Each bullet: 1-2 sentences, action-oriented, no fluff
- Write in English
- Use the exact section format provided"""

def build_crypto_prompt(articles: list[dict]) -> str:
    headlines = "\n".join(
        f"- {a['title']}: {a['summary'][:150]}" for a in articles[:60]
    )
    return f"""Here are today's crypto news headlines:

{headlines}

Write a digest with EXACTLY these sections (skip a section if no relevant news, never invent news):

🔥 TOP NARRATIVES
(2-3 bullets on major themes — tech, regulation, adoption)

🏦 INSTITUTIONAL MOVES
(2-3 bullets on institutional/enterprise/government crypto activity)

⚡ BREAKING & MARKET EVENTS
(2-3 bullets on significant protocol events, hacks, launches — NO price news)

📈 MARKET & PRODUCT NARRATIVES
(2-3 bullets on RWA, DeFi, tokenization narratives and product developments)

📊 INDUSTRY REPORTS & EVENTS
(2-3 bullets on research, conferences, policy, regulatory updates)

💡 KEY TAKEAWAY
(1 sentence: the single most important insight of the day)

Important: SKIP any bullet that is about price movements, token crashes, surges, ATH, or liquidations."""

def build_competitor_prompt(competitor_news: dict[str, list[dict]]) -> str:
    lines = []
    for project, items in competitor_news.items():
        lines.append(f"\n### {project}")
        for item in items:
            lines.append(f"- {item['title']}: {item['summary'][:150]}")
    
    headlines = "\n".join(lines)
    return f"""Here are Google News results for these crypto competitor projects:
{headlines}

Write a "🔍 COMPETITOR WATCH" section.
Rules:
- Only include news with real signal: partnership, product launch, RWA deal, protocol upgrade, institutional integration, major hire
- Ignore: price news, speculation, opinion pieces, rehashed old news
- Format: "[Project]: [what happened in 1 sentence]"
- Max 4-5 bullets total across all projects
- If no meaningful news for a project, skip it entirely
- If overall nothing meaningful, write: "No significant competitor updates today."

Output ONLY the section content, no header."""

def call_claude(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()

# ── BUILD FINAL MESSAGE ───────────────────────────────────────────────────────
def build_digest() -> str:
    logger.info("Fetching news…")
    crypto_articles   = fetch_all_crypto_news()
    competitor_news   = fetch_competitor_news()

    logger.info("Calling Claude for main digest…")
    main_digest = call_claude(build_crypto_prompt(crypto_articles))

    logger.info("Calling Claude for competitor watch…")
    competitor_section = call_claude(build_competitor_prompt(competitor_news))

    vn_time = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%A, %B %d %Y")

    message = (
        f"📰 *CRYPTO NEWS DIGEST*\n"
        f"📅 {date_str}\n"
        f"{'─' * 30}\n\n"
        f"{main_digest}\n\n"
        f"{'─' * 30}\n\n"
        f"🔍 *COMPETITOR WATCH*\n"
        f"{competitor_section}"
    )
    return message

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram message limit is 4096 chars; split if needed
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    success = True
    for chunk in chunks:
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       chunk,
            "parse_mode": "Markdown",
        }, timeout=30)
        if not resp.ok:
            logger.error(f"Telegram error: {resp.text}")
            success = False
    return success

# ── JOB ───────────────────────────────────────────────────────────────────────
def run_job():
    logger.info("Starting daily digest job…")
    try:
        digest = build_digest()
        ok = send_telegram(digest)
        logger.info(f"Digest sent: {ok}")
    except Exception as e:
        logger.error(f"Job failed: {e}", exc_info=True)
        send_telegram(f"⚠️ Bot error: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Bot starting…")

    if RUN_ON_START:
        logger.info("RUN_ON_START=true → running immediately")
        run_job()

    # Schedule 8:00 AM Vietnam time (UTC+7)
    schedule.every().day.at("01:00").do(run_job)  # 01:00 UTC = 08:00 VN
    logger.info("Scheduled daily digest at 08:00 ICT (01:00 UTC)")

    while True:
        schedule.run_pending()
        time.sleep(30)
