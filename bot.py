import os
import feedparser
import requests
import anthropic
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]

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
    "federal reserve", "treasury",
    "rwa", "real world asset", "tokenization", "defi", "layer 2", "l2",
    "bitcoin", "ethereum", "solana", "stablecoin", "usdc", "usdt",
    "hack", "exploit", "crash", "surge", "ath", "all-time high",
    "liquidation", "bankruptcy", "partnership", "acquisition", "launch",
    "mainnet", "upgrade", "halving", "regulation", "ban", "approval",
    "listing", "delisting", "airdrop",
]

def fetch_news(max_per_feed=8):
    articles = []
    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:max_per_feed]:
                title   = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))[:300]
                link    = entry.get("link", "")
                combined = (title + " " + summary).lower()
                if any(kw in combined for kw in IMPORTANT_KEYWORDS):
                    articles.append({"source": feed_info["name"], "title": title, "summary": summary, "link": link})
        except Exception as e:
            logger.warning(f"Failed to fetch {feed_info['name']}: {e}")
    logger.info(f"Fetched {len(articles)} relevant articles")
    return articles

def summarize_with_claude(articles):
    if not articles:
        return "Không có tin tức quan trọng hôm nay."
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    articles_text = "\n\n".join([
        f"[{a['source']}] {a['title']}\n{a['summary']}\nLink: {a['link']}"
        for a in articles[:30]
    ])
    prompt = f"""Mày là chuyên gia phân tích thị trường crypto. Dưới đây là các tin tức crypto mới nhất hôm nay.

Hãy tổng hợp thành một bản tin ngắn gọn, súc tích bằng tiếng Việt theo format sau:

📰 *CRYPTO NEWS DIGEST*
📅 [ngày hôm nay]

🔥 *TOP NARRATIVES*
[3-5 narrative/theme đang nổi bật nhất, mỗi cái 1-2 dòng]

🏦 *INSTITUTIONAL MOVES*
[Các động thái của tổ chức lớn: ETF, đầu tư, quy định... nếu có]

⚡ *BREAKING & MARKET EVENTS*
[2-3 sự kiện chấn động nhất, đang được thảo luận nhiều]

📊 *INDUSTRY REPORTS & EVENTS*
[Báo cáo, sự kiện ngành đáng chú ý nếu có]

💡 *KEY TAKEAWAY*
[1-2 dòng tóm gọn: thị trường đang quan tâm điều gì nhất hôm nay]

Yêu cầu:
- Ngắn gọn, đi thẳng vào vấn đề
- Dùng tiếng Việt, thuật ngữ kỹ thuật giữ nguyên tiếng Anh
- Mỗi mục tối đa 3-4 bullet points
- Có link nguồn cho tin quan trọng nhất
- Format Markdown cho Telegram (dùng *bold*, không dùng **)

Tin tức:
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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
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
        digest   = summarize_with_claude(articles)
        send_to_telegram(digest)
        logger.info("✅ Digest job completed")
    except Exception as e:
        logger.error(f"❌ Digest job failed: {e}")
        send_to_telegram(f"⚠️ Bot gặp lỗi khi tạo digest: {str(e)}")

def main():
    if os.environ.get("RUN_ON_START", "false").lower() == "true":
        run_digest()
    vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
    scheduler = BlockingScheduler(timezone=vn_tz)
    scheduler.add_job(run_digest, CronTrigger(day_of_week="mon,wed,fri", hour=8, minute=0, timezone=vn_tz))
    logger.info("⏰ Scheduler started — running Mon/Wed/Fri at 08:00 ICT")
    scheduler.start()

if __name__ == "__main__":
    main()
