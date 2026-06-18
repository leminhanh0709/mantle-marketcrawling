# crypto news bot v1.2
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
