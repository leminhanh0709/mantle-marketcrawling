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
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY", "")
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

# ── RESEARCH ACCOUNTS ─────────────────────────────────────────────────────────
RESEARCH_ACCOUNTS = {
    "galaxyhq":       "Galaxy",
    "glxyresearch":   "Galaxy Research",
    "MessariCrypto":  "Messari",
    "a16zcrypto":     "a16z Crypto",
    "coinbase":       "Coinbase",
    "BinanceResearch":"Binance Research",
    "chainalysis":    "Chainalysis",
}

# ── NEWS RSS FEEDS ────────────────────────────────────────────────────────────
NEWS_FEEDS = [
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

def fetch_x_tweets(username: str, user_id: str, max_results: int = 10, hours: int = 24) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        tweets.append({"text": text[:280], "link": link, "impressions": impressions})
    return tweets

def fetch_all_competitor_tweets() -> list[dict]:
    all_tweets = []
    for username, display_name in COMPETITORS.items():
        user_id = get_x_user_id(username)
        if not user_id:
            continue
        tweets = fetch_x_tweets(username, user_id, max_results=10, hours=24)
        for t in tweets:
            t["project"] = display_name
        all_tweets.extend(tweets)
        time.sleep(0.5)
    logger.info(f"Fetched {len(all_tweets)} competitor tweets")
    return all_tweets

def fetch_research_tweets() -> list[dict]:
    all_tweets = []
    for username, display_name in RESEARCH_ACCOUNTS.items():
        user_id = get_x_user_id(username)
        if not user_id:
            continue
        tweets = fetch_x_tweets(username, user_id, max_results=10, hours=48) 
        for t in tweets:
            t["source"] = display_name
        all_tweets.extend(tweets)
        time.sleep(0.5)
    logger.info(f"Fetched {len(all_tweets)} research tweets")
    return all_tweets

def format_impressions(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

# ── RSS FETCH ─────────────────────────────────────────────────────────────────
def fetch_feed(url: str, max_items: int = 8, days: int = 1) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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

def fetch_news() -> list[dict]:
    articles = []
    for url in NEWS_FEEDS:
        articles.extend(fetch_feed(url, max_items=5, days=1))
    logger.info(f"Fetched {len(articles)} news articles")
    return articles[:40]

# ── SUPABASE ──────────────────────────────────────────────────────────────────
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

def get_sent_research_links() -> set[str]:
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/sent_research?select=link",
            headers=SUPABASE_HEADERS,
            timeout=10,
        )
        if resp.ok:
            return {row["link"] for row in resp.json()}
    except Exception as e:
        logger.warning(f"Supabase fetch failed: {e}")
    return set()

def save_sent_research_links(links: list[str]) -> None:
    if not links:
        return
    try:
        rows = [{"link": link} for link in links]
        requests.post(
            f"{SUPABASE_URL}/rest/v1/sent_research",
            headers={**SUPABASE_HEADERS, "Prefer": "ignore-duplicates"},
            json=rows,
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Supabase save failed: {e}")

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
    sorted_tweets = sorted(all_tweets, key=lambda t: t.get("impressions", 0), reverse=True)[:20]
    lines = "\n".join(
        f"{i}: [{t['project']}] {t['text'][:150]} | impressions={t['impressions']} | {t['link']}"
        for i, t in enumerate(sorted_tweets)
    )
    return f"""From these tweets (already sorted by impressions), pick the TOP 5. Skip price/hype/meme/teaser tweets — only include: product launches, partnerships, protocol upgrades, RWA, institutional moves, ecosystem news.

Tweets:
{lines}

Output EXACTLY 5 lines ranked 1 to 5 by impressions:
RANK | PROJECT | NARRATIVE | One sentence summary (max 12 words) | link | impressions_count

Narratives: {NARRATIVES}
Output lines only."""

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
            f"[{item['summary']}]({item['link']}) — {imp_str} views"
        )
    return "\n\n".join(lines) if lines else "No significant posts today."

# ── SECTION 2: MEDIA COVERAGE ─────────────────────────────────────────────────
def build_media_coverage_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{i}: {a['title']} | {a['link']}" for i, a in enumerate(articles))
    return f"""From these crypto news headlines, pick the TOP 5 most impactful CURRENT EVENTS — regulatory decisions, institutional deals, protocol launches, partnerships, industry moves. Time-sensitive news that affects the market immediately.

DO NOT include: research reports, data analyses, opinion pieces, market outlooks.

Score each on impact (1-10):
- Market-wide impact vs single project
- Credibility and novelty
- Actionable/immediate significance

{lines}

Output EXACTLY 5 lines ranked 1 to 5:
RANK | One sentence summary (max 10 words) | link

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
                "rank":    parts[0],
                "summary": parts[1],
                "link":    parts[2],
            })
    return items

def format_media_coverage_block(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(f"{item['rank']}. [{item['summary']}]({item['link']})")
    return "\n".join(lines) if lines else "No significant market news today."

# ── SECTION 3: RESEARCH & REPORTS ────────────────────────────────────────────
def build_research_prompt(tweets: list[dict], sent_links: set[str]) -> str:
    fresh = [t for t in tweets if t["link"] not in sent_links]
    if not fresh:
        return ""
    lines = "\n".join(
        f"{i}: [{t['source']}] {t['text'][:200]} | {t['link']}"
        for i, t in enumerate(fresh)
    )
    return f"""You are curating a weekly research briefing for C-level executives and institutional decision makers in crypto/blockchain.

From these tweets by research firms (Galaxy, Messari, a16z, Coinbase, Binance Research, Chainalysis), pick the TOP 3 that share or reference STRATEGIC RESEARCH REPORTS — macro-level insights relevant to business or investment decisions.

ONLY accept tweets that:
- Share a research report, analysis, or data study
- Discuss macro market structure, institutional adoption, tokenization trends
- Reference state-of-industry data or regulatory outlook
- Contain a link to a full report or research piece

STRICTLY REJECT:
- Price commentary or market moves
- Event announcements or conference promos
- Generic opinion without data
- Teaser tweets without substance

Relevant narratives: {NARRATIVES}

Tweets:
{lines}

Output ranked lines (1 to max 3):
RANK | SOURCE | NARRATIVE | Short strategic title (max 10 words) | link

Output lines only, no extra text."""

def parse_research(raw: str) -> list[dict]:
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5:
            items.append({
                "rank":      parts[0],
                "source":    parts[1],
                "narrative": parts[2],
                "title":     parts[3],
                "link":      parts[4],
            })
    return items

def format_research_block(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(
            f"{item['rank']}. *{item['source']}* `{item['narrative']}`\n"
            f"[{item['title']}]({item['link']})"
        )
    return "\n\n".join(lines) if lines else "No notable research this week."

# ── BUILD DIGEST ──────────────────────────────────────────────────────────────
def build_digest() -> tuple[list[str], dict]:
    logger.info("Fetching competitor tweets…")
    competitor_tweets = fetch_all_competitor_tweets()

    logger.info("Fetching research tweets…")
    research_tweets = fetch_research_tweets()

    logger.info("Fetching news…")
    news_articles = fetch_news()

    logger.info("Calling Claude (3 calls)…")
    raw_outstanding = call_claude(build_outstanding_posts_prompt(competitor_tweets), max_tokens=800)
    raw_media       = call_claude(build_media_coverage_prompt(news_articles), max_tokens=600)

    sent_links      = get_sent_research_links()
    research_prompt = build_research_prompt(research_tweets, sent_links)
    if research_prompt:
        raw_research   = call_claude(research_prompt, max_tokens=500)
        research_items = parse_research(raw_research)
        save_sent_research_links([item["link"] for item in research_items])
    else:
        research_items = []

    outstanding_items = parse_outstanding_posts(raw_outstanding)
    media_items       = parse_media_coverage(raw_media)

    outstanding_block = format_outstanding_block(outstanding_items)
    media_block       = format_media_coverage_block(media_items)
    research_block    = format_research_block(research_items)

    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")
    header   = f"📰 *CRYPTO NEWS DIGEST* — {date_str}\n{'─' * 28}\n\n"

    messages = [
        header + f"📢 *OUTSTANDING INDUSTRY POSTS*\n\n{outstanding_block}",
        f"📡 *MEDIA COVERAGE*\n\n{media_block}",
        f"📊 *RESEARCH & REPORTS*\n\n{research_block}",
    ]

    sections = {
        "outstanding": outstanding_items,
        "media":       media_items,
        "research":    research_items,
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
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "📡 **MEDIA COVERAGE**"}})
    elements.append({"tag": "hr"})
    media_lines = "\n".join(
        f"{item['rank']}. [{item['summary']}]({item['link']})"
        for item in digest_sections.get("media", [])
    )
    if media_lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": media_lines}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "📊 **RESEARCH & REPORTS**"}})
    elements.append({"tag": "hr"})
    research_lines = []
    for item in digest_sections.get("research", []):
        research_lines.append(f"{item['rank']}. **{item['source']}** - {item['narrative']}\n[{item['title']}]({item['link']})")
    if research_lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(research_lines)}})

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
        lark_card = build_lark_card(sections)
        send_lark(lark_card)
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
