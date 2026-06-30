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
        tweets.append({"text": text[:280], "link": link, "impressions": impressions})
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
        time.sleep(0.5)
    logger.info(f"Fetched X tweets for {len(result)} competitors")
    return result

def build_top_posts(competitor_tweets: dict[str, list[dict]], top_n: int = 2) -> list[dict]:
    """For each chain, pick top N tweets by impression count."""
    result = []
    for project, tweets in competitor_tweets.items():
        sorted_tweets = sorted(tweets, key=lambda t: t.get("impressions", 0), reverse=True)
        for t in sorted_tweets[:top_n]:
            title = t["text"].split("\n")[0].strip()
            if len(title) > 70:
                title = title[:70].rsplit(" ", 1)[0] + "…"
            result.append({
                "project":     project,
                "title":       title,
                "link":        t["link"],
                "impressions": t.get("impressions", 0),
            })
    return result

def build_top_posts_prompt(top_posts: list[dict]) -> str:
    lines = "\n".join(f"{i}: [{p['project']}] {p['title']}" for i, p in enumerate(top_posts))
    return f"""Summarize each tweet below into ONE short, clear sentence (max 12 words) explaining what it announces or means. If a tweet is vague, cryptic, or meaningless (like teasers, emojis, "shhhh"), infer the likely meaning briefly or write "Teaser post — no clear details yet."

{lines}

Output EXACTLY one line per index, in this format:
INDEX: summary sentence

Output lines only, no extra text."""

def summarize_top_posts(top_posts: list[dict]) -> list[dict]:
    if not top_posts:
        return []
    prompt = build_top_posts_prompt(top_posts)
    raw = call_claude(prompt, max_tokens=600)
    summary_map = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        idx_str, summary = line.split(":", 1)
        try:
            idx = int(idx_str.strip())
            summary_map[idx] = summary.strip()
        except ValueError:
            continue
    for i, p in enumerate(top_posts):
        p["summary"] = summary_map.get(i, p["title"])
    return top_posts

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
- [Short title max 10 words](link)

3 bullets only."""

def build_breaking_prompt(articles: list[dict]) -> str:
    lines = "\n".join(f"{a['title']} | {a['link']}" for a in articles)
    return f"""Pick 3 biggest breaking crypto events with wide market impact (hacks, regulation, major protocol events, industry shifts).
No price/token crash news.

{lines}

Format (Markdown hyperlink):
- [Short title max 10 words](link)

3 bullets only."""

# ── FORMAT & PARSE ────────────────────────────────────────────────────────────
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
    return "\n".join(lines) if lines else "No significant updates today."

def parse_bullets(raw: str) -> list[dict]:
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip().lstrip("•-*[").strip()
        if not line:
            continue
        if line.startswith("[") and "](" in line and line.endswith(")"):
            title = line[1:line.index("](")]
            link  = line[line.index("](")+2:-1]
            items.append({"title": title, "link": link})
        elif " — " in line:
            parts = line.split(" — ", 1)
            items.append({"title": parts[0].strip(), "link": parts[1].strip()})
        else:
            items.append({"title": line, "link": ""})
    return items

def parse_competitor_lines(raw: str) -> list[dict]:
    items = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4:
            items.append({
                "project":   parts[0],
                "narrative": parts[1],
                "title":     parts[2],
                "link":      parts[3],
            })
    return items

# ── BUILD DIGEST ──────────────────────────────────────────────────────────────
def build_digest() -> tuple[str, dict]:
    logger.info("Fetching X tweets…")
    competitor_tweets = fetch_all_competitor_tweets()

    logger.info("Fetching RSS news…")
    crypto_articles = fetch_crypto_news()

    logger.info("Calling Claude…")
    raw_competitors   = call_claude(build_competitor_prompt(competitor_tweets), max_tokens=800)
    raw_institutional = call_claude(build_institutional_prompt(crypto_articles), max_tokens=400)
    raw_breaking      = call_claude(build_breaking_prompt(crypto_articles), max_tokens=400)

    competitor_items    = parse_competitor_lines(raw_competitors)
    institutional_items = parse_bullets(raw_institutional)
    breaking_items      = parse_bullets(raw_breaking)
    competitor_block    = format_competitor_block(raw_competitors)
    top_posts           = build_top_posts(competitor_tweets, top_n=1)
    top_posts           = summarize_top_posts(top_posts)

    top_posts_by_project = {}
    for p in top_posts:
        top_posts_by_project.setdefault(p["project"], []).append(p)

    top_posts_lines = []
    for project, posts in top_posts_by_project.items():
        top_posts_lines.append(f"*{project}*")
        for p in posts:
            top_posts_lines.append(f"  • [{p.get('summary', p['title'])}]({p['link']}) — {format_impressions(p['impressions'])} views")
    top_posts_block = "\n".join(top_posts_lines) if top_posts_lines else "No data available today."

    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")

    telegram_text = (
        f"📰 *CRYPTO NEWS DIGEST* — {date_str}\n"
        f"{'─' * 28}\n\n"
        f"🔍 *NARRATIVES BY CHAINS*\n\n"
        f"{competitor_block}\n\n"
        f"{'─' * 28}\n\n"
        f"🏆 *TOP PERFORMING POSTS*\n\n"
        f"{top_posts_block}\n\n"
        f"{'─' * 28}\n\n"
        f"🏦 *INSTITUTIONAL MOVES*\n\n"
        f"{raw_institutional}\n\n"
        f"{'─' * 28}\n\n"
        f"⚡ *BREAKING & MARKET EVENTS*\n\n"
        f"{raw_breaking}"
    )

    sections = {
        "competitors":   competitor_items,
        "top_posts":     top_posts,
        "institutional": institutional_items,
        "breaking":      breaking_items,
    }

    return telegram_text, sections

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    divider = "─" * 28
    raw_parts = text.split(divider)

    chunks = []
    current = ""
    for part in raw_parts:
        candidate = current + (divider if current else "") + part
        if len(candidate) > 3500:
            if current:
                chunks.append(current.strip())
            current = part
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())

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
            resp2 = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            }, timeout=30)
            if not resp2.ok:
                logger.error(f"Telegram retry error: {resp2.text}")
                ok = False
    return ok

# ── LARK ──────────────────────────────────────────────────────────────────────
def build_lark_card(digest_sections: dict) -> dict:
    vn_time  = datetime.now(timezone(timedelta(hours=7)))
    date_str = vn_time.strftime("%d/%m/%Y")
    elements = []

    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "🔍 **NARRATIVES BY CHAINS**"}})
    elements.append({"tag": "hr"})
    competitor_content = "\n".join(
        f"**{item['project']}** - {item['narrative']}\n[{item['title']}]({item['link']})"
        for item in digest_sections.get("competitors", [])
    )
    if competitor_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": competitor_content}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "🏆 **TOP PERFORMING POSTS**"}})
    elements.append({"tag": "hr"})
    top_posts = digest_sections.get("top_posts", [])
    top_posts_by_project = {}
    for p in top_posts:
        top_posts_by_project.setdefault(p["project"], []).append(p)
    top_posts_lines = []
    for project, posts in top_posts_by_project.items():
        top_posts_lines.append(f"**{project}**")
        for p in posts:
            top_posts_lines.append(f"[{p.get('summary', p['title'])}]({p['link']}) — {format_impressions(p['impressions'])} views")
    top_posts_content = "\n".join(top_posts_lines)
    if top_posts_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": top_posts_content}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "🏦 **INSTITUTIONAL MOVES**"}})
    elements.append({"tag": "hr"})
    institutional_content = "\n".join(
        f"[{item['title']}]({item['link']})"
        for item in digest_sections.get("institutional", [])
    )
    if institutional_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": institutional_content}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "⚡ **BREAKING & MARKET EVENTS**"}})
    elements.append({"tag": "hr"})
    breaking_content = "\n".join(
        f"[{item['title']}]({item['link']})"
        for item in digest_sections.get("breaking", [])
    )
    if breaking_content:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": breaking_content}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📰 CRYPTO NEWS DIGEST — {date_str}"},
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
        telegram_text, sections = build_digest()
        send_telegram(telegram_text)
        # Lark temporarily disabled for testing
        # lark_card = build_lark_card(sections)
        # send_lark(lark_card)
        logger.info("Digest sent to Telegram")
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
