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
LARK_WEBHOOK_URL   = os.environ.get("LARK_WEBHOOK_URL", "")
RUN_ON_START       = os.environ.get("RUN_ON_START", "false").lower() == "true"

# ── COMPETITORS ───────────────────────────────────────────────────────────────
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
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://api.twitter.com/2/users/{user_id}/tweets"
    params = {
        "max_results":  max_results,
        "start_time":   since,
        "tweet.fields": "created_at,text,entities,public_metrics",
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
        public_metrics = tweet.get("public_metrics", {})
        impressions = public_metrics.get("impression_count", 0)
        tweets.append({"text": text[:280], "link": link, "impressions": impressions, "project": ""})
    return tweets

def fetch_all_competitor_tweets() -> list[dict]:
    all_tweets = []
    for username, display_name in COMPETITORS.items():
        user_id = get_x_user_id(username)
        if not user_id:
            continue
        tweets = fetch_x_tweets(username, user_id, max_results=10)
        for t in tweets:
            t["project"] = display_name
        all_tweets.extend(tweets)
        time.sleep(0.5)
    logger.info(f"Fetched {len(all_tweets)} total competitor tweets")
    return all_tweets

def format_impressions(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

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
    return articles[:40]

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

# ── SECTION 1: OUTSTANDING INDUSTRY POSTS ────────────────────────────────────
def build_outstanding_posts_prompt(all_tweets: list[dict]) -> str:
    lines = "\n".join(
        f"{i}: [{t['project']}] {t['text'][:150]} | impressions={t['impressions']} | {t['link']}"
        for i, t in enumerate(all_tweets)
    )
    return f"""You are analyzing tweets from crypto competitor projects. Pick the TOP 5 most impactful posts overall (not per chain).

Score each tweet on impact (1-10) based on:
- Market significance (affects whole industry vs one project)
- Novelty (new announcement vs repeated info)
- Concrete action (real deal/number/launch vs vague teaser)
- Source credibility

Final rank = (impression_score * 0.4) + (impact_score * 0.6)
Where impression_score = normalized 1-10 from impressions count.

Tweets:
{lines}

Output EXACTLY 5 lines, ranked 1 to 5 by final score:
RANK | PROJECT | NARRATIVE | One sentence summary (max 12 words) | link | impressions_count

Narratives: {NARRATIVES}
Skip price/hype/meme tweets. Output lines only."""

def parse_outstanding_posts(raw: str) -> list[dict]:
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5:
            items.append({
                "rank":        parts[0],
                "project":     parts[1],
                "narrative":   parts[2],
                "summary":     parts[3],
                "link":        parts[4],
                "impressions": parts[5] if len(parts) > 5 else "0",
            })
    return items

def format_outstanding_block(items: list[dict]) -> str:
    lines = []
    for item in items:
        try:
            imp = int(item["impressions"].replace(",", ""))
            imp_str = format_impressions(imp)
        except Exception:
            imp_str = item["impressions"]
        lines.append(
            f"{item['rank']}. *{item['project']}* `{item['narrative']}`\n"
            f"   [{item['summary']}]({item['link']}) — {imp_str} views"
        )
    return "\n\n".join(lines) if lines else "No significant posts today."

# ── SECTION 2: MARKET INTELLIGENCE ───────────────────────────────────────────
def build_market_intelligence_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{i}: {a['title']} | {a['link']}" for i, a in enumerate(articles))
    return f"""From these crypto news headlines, pick the TOP 5 most impactful stories combining institutional moves, breaking events, regulation, and market shifts.

Score each on impact (1-10):
- Market-wide impact vs single project
- Credibility and novelty
- Actionable information

{lines}

Output EXACTLY 5 lines ranked 1 to 5:
RANK | One sentence summary (max 10 words) | link

Output lines only, no extra text."""

def parse_market_intelligence(raw: str) -> list[dict]:
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            items.append({
                "rank":    parts[0],
                "summary": parts[1],
                "link":    parts[2],
            })
    return items

def format_market_intelligence_block(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(f"{item['rank']}. [{item['summary']}]({item['link']})")
    return "\n".join(lines) if lines else "No significant market news today."

# ── SECTION 3: MEDIA COVERAGE ─────────────────────────────────────────────────
def build_media_coverage_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{i}: {a['title']} | {a['link']}" for i, a in enumerate(articles))
    return f"""From these crypto news headlines, pick the TOP 3 most important reports, research, or in-depth analyses (not breaking news — focus on reports, studies, data releases, market analyses).

{lines}

Output EXACTLY 3 lines ranked 1 to 3:
RANK | Short title (max 10 words) | link

Output lines only, no extra text."""

def parse_media_coverage(raw: str) -> list[dict]:
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            items.append({
                "rank":  parts[0],
                "title": parts[1],
                "link":  parts[2],
            })
    return items

def format_media_coverage_block(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(f"{item['rank']}. [{item['title']}]({item['link']})")
    return "\n".join(lines) if lines else "No notable reports today."

# ── BUILD DIGEST ──────────────────────────────────────────────────────────────
def build_digest() -> tuple[list[str], dict]:
    logger.info("Fetching X tweets…")
    all_tweets = fetch_all_competitor_tweets()

    logger.info("Fetching RSS news…")
    crypto_articles = fetch_crypto_news()

    logger.info("Calling Claude (3 calls)…")
    raw_outstanding  = call_claude(build_outstanding_posts_prompt(all_tweets), max_tokens=800)
    raw_intelligence = call_claude(build_market_intelligence_prompt(crypto_articles), max_tokens=600)
    raw_media        = call_claude(build_media_coverage_prompt(crypto_articles), max_tokens=400)

    outstanding_items  = parse_outstanding_posts(raw_outstanding)
    intelligence_items = parse_market_intelligence(raw_intelligence)
    media_items        = parse_media_coverage(raw_media)

    outstanding_block  = format_outstanding_block(outstanding_items)
    intelligence_block = format_market_intelligence_block(intelligence_items)
    media_block        = format_media_coverage_block(media_items)

    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")
    header   = f"📰 *CRYPTO NEWS DIGEST* — {date_str}\n{'─' * 28}\n\n"

    messages = [
        header + f"📢 *OUTSTANDING INDUSTRY POSTS*\n\n{outstanding_block}",
        f"📡 *MARKET INTELLIGENCE*\n\n{intelligence_block}",
        f"📰 *MEDIA COVERAGE*\n\n{media_block}",
    ]

    sections = {
        "outstanding":  outstanding_items,
        "intelligence": intelligence_items,
        "media":        media_items,
    }

    return messages, sections

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }, timeout=30)
    if not resp.ok:
        logger.error(f"Telegram error: {resp.text}")
        resp2 = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    text,
            "disable_web_page_preview": True,
        }, timeout=30)
        if not resp2.ok:
            logger.error(f"Telegram retry error: {resp2.text}")
            return False
    return True

def send_telegram_messages(messages: list[str]) -> bool:
    ok = True
    for msg in messages:
        if len(msg) > 4000:
            parts = msg.split("\n\n")
            current = ""
            for part in parts:
                candidate = current + ("\n\n" if current else "") + part
                if len(candidate) > 3800:
                    if current:
                        ok = send_telegram_message(current.strip()) and ok
                    current = part
                else:
                    current = candidate
            if current.strip():
                ok = send_telegram_message(current.strip()) and ok
        else:
            ok = send_telegram_message(msg) and ok
        time.sleep(0.3)
    return ok

# ── LARK ──────────────────────────────────────────────────────────────────────
def build_lark_card(digest_sections: dict) -> dict:
    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")
    elements = []

    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "📢 **OUTSTANDING INDUSTRY POSTS**"}})
    elements.append({"tag": "hr"})
    outstanding_lines = []
    for item in digest_sections.get("outstanding", []):
        outstanding_lines.append(f"{item['rank']}. **{item['project']}** - {item['narrative']}\n[{item['summary']}]({item['link']})")
    if outstanding_lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(outstanding_lines)}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "📡 **MARKET INTELLIGENCE**"}})
    elements.append({"tag": "hr"})
    intel_lines = "\n".join(
        f"{item['rank']}. [{item['summary']}]({item['link']})"
        for item in digest_sections.get("intelligence", [])
    )
    if intel_lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": intel_lines}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "📰 **MEDIA COVERAGE**"}})
    elements.append({"tag": "hr"})
    media_lines = "\n".join(
        f"{item['rank']}. [{item['title']}]({item['link']})"
        for item in digest_sections.get("media", [])
    )
    if media_lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": media_lines}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": f"📰 CRYPTO NEWS DIGEST — {date_str}"},
                "template": "blue"
            },
            "elements": elements
        }
    }

def send_lark(card: dict) -> bool:
    if not LARK_WEBHOOK_URL:
        logger.info("Lark webhook not configured, skipping")
        return False
    resp = requests.post(LARK_WEBHOOK_URL, json=card, timeout=30)
    if not resp.ok:
        logger.error(f"Lark error: {resp.text}")
        return False
    result = resp.json()
    if result.get("code", 0) != 0:
        logger.error(f"Lark API error: {result}")
        return False
    logger.info("Lark card sent")
    return True

# ── JOB ───────────────────────────────────────────────────────────────────────
def run_job():
    logger.info("Running digest…")
    try:
        messages, sections = build_digest()
        send_telegram_messages(messages)
        # Lark disabled for testing — enable when final
        # lark_card = build_lark_card(sections)
        # send_lark(lark_card)
        logger.info("Digest sent to Telegram (3 messages)")
    except Exception as e:
        logger.error(f"Job failed: {e}", exc_info=True)
        send_telegram_message(f"⚠️ Bot error: {e}")

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
