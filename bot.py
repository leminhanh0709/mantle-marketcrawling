# crypto news bot v1.3
import os
import feedparser
import requests
import anthropic
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN")
if not TELEGRAM_CHAT_ID:
    raise ValueError("Missing TELEGRAM_CHAT_ID")
if not ANTHROPIC_API_KEY:
    raise ValueError("Missing ANTHROPIC_API_KEY")

RSS_FEEDS = [
    {"name": "CoinDesk",        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "CoinTelegraph",   "url": "https://cointelegraph.com/rss"},
    {"name": "The Block",       "url": "https://www.theblock.co/rss.xml"},
    {"name": "Decrypt",         "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin Magazine","url": "https://bitcoinmagazine.com/.rss/full/"},
    {"name": "DL News",         "url": "https://www.dlnews.com/arc/outboundfeeds/rss/"},
]

IMPORTANT_KEYWORDS = [
    "blackrock", "fidelity", "grayscale", "microstrategy", "jpmorgan",
    "goldman sachs", "morgan stanley", "etf", "sec", "cftc", "fed",
    "federal reserve", "treasury", "rwa", "real world asset",
    "tokenization", "defi", "layer 2", "l2", "bitcoin", "ethereum",
    "solana", "stablecoin", "usdc", "usdt", "hack", "exploit",
    "crash", "surge", "ath", "all-time high", "liquidation",
    "bankruptcy", "partnership", "acquisition", "launch", "mainnet",
    "upgrade", "halving", "regulation", "ban", "approval",
    "listing", "delisting", "airdrop",
]


def fetch_news(max_per_feed=8):
    articles = []
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))[:300]
                link = entry.get("link", "")
                combined = (title + " " + summary).lower()
                if any(kw in combined for kw in IMPORTANT_KEYWORDS):
                    articles.append({
                        "source": feed_info["name"],
                        "title": title,
                        "summary": summary,
                        "link": link,
                    })
        except Exception as e:
            logger.warning(f"Failed to fetch {feed_info['name']}: {e}")
    logger.info(f"Fetched {len(articles)} relevant articles")
    return articles


def summarize_with_claude(articles):
    if not articles:
        return "No important news today."
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    articles_text = "\n\n".join([
        f"[{a['source']}] {a['title']}\n{a['summary']}\nLink: {a['link']}"
        for a in articles[:30]
    ])
    prompt = f"""You are a professional crypto market analyst. Below are the latest crypto news articles.

Summarize them into a concise, informative digest in English using this format:

📰 *CRYPTO NEWS DIGEST*
📅 [today's date]

🔥 *TOP NARRATIVES*
- [Narrative 1 - 1-2 lines] — [Source Name](link)
- [Narrative 2 - 1-2 lines] — [Source Name](link)
- [Narrative 3 - 1-2 lines] — [Source Name](link)

🏦 *INSTITUTIONAL MOVES*
- [Institution action - 1-2 lines] — [Source Name](link)
- [Institution action - 1-2 lines] — [Source Name](link)

⚡ *BREAKING & MARKET EVENTS*
- [Event 1 - 1-2 lines] — [Source Name](link)
- [Event 2 - 1-2 lines] — [Source Name](link)

📊 *INDUSTRY REPORTS & EVENTS*
- [Report/Event - 1-2 lines] — [Source Name](link)

💡 *KEY TAKEAWAY*
[1-2 sentences summarizing what the market is focused on today]

Requirements:
- Write in English, keep technical terms as-is
- Max 3-4 bullet points per section
- Every bullet point MUST include a source link in format: — [Source Name](url)
- If a section has no relevant news, skip it entirely
- Use Telegram Markdown format (*bold*, no **)
- Be concise and direct

News articles:
{articles_text}
"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=30)
    if response.status_code == 200:
        logger.info("✅ Message sent to Telegram successfully")
    else:
        logger.error(f"❌ Telegram error: {response.status_code} — {response.text}")
        payload["parse_mode"] = ""
        requests.post(url, json=payload, timeout=30)


def run_digest():
    logger.info("🚀 Starting crypto news digest job...")
    try:
        articles = fetch_news()
        digest = summarize_with_claude(articles)
        send_to_telegram(digest)
        logger.info("✅ Digest job completed")
    except Exception as e:
        logger.error(f"❌ Digest job failed: {e}")
        send_to_telegram(f"⚠️ Bot error: {str(e)}")


def main():
    logger.info("🤖 Bot starting up...")
    logger.info(f"TELEGRAM_BOT_TOKEN: {'set' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
    logger.info(f"TELEGRAM_CHAT_ID: {'set' if TELEGRAM_CHAT_ID else 'MISSING'}")
    logger.info(f"ANTHROPIC_API_KEY: {'set' if ANTHROPIC_API_KEY else 'MISSING'}")

    if os.environ.get("RUN_ON_START", "false").lower() == "true":
        logger.info("RUN_ON_START=true → running digest now...")
        run_digest()

    vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
    scheduler = BlockingScheduler(timezone=vn_tz)
    scheduler.add_job(
        run_digest,
        CronTrigger(hour=8, minute=0, timezone=vn_tz),
    )
    logger.info("⏰ Scheduler started — running every day at 08:00 ICT")
    scheduler.start()


if __name__ == "__main__":
    main()
