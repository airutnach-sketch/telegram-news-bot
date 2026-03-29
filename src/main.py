import hashlib
import html
import os
import re
import sqlite3
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "seen.db")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
USER_AGENT = "Mozilla/5.0 (compatible; TelegramNewsMonitor/1.0)"

KEYWORDS_ENV = os.getenv("KEYWORDS", "გიორგი გახარია, გახარია, პარტია საქართველოსთვის")
KEYWORDS = [k.strip() for k in re.split(r"[,\n;]+", KEYWORDS_ENV) if k.strip()]

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
BACKFILL_HOURS = int(os.getenv("BACKFILL_HOURS", "24"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "25"))

if not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not CHANNEL_ID:
    raise RuntimeError("Missing TELEGRAM_CHANNEL_ID")
if not KEYWORDS:
    raise RuntimeError("Missing KEYWORDS")

# Google News RSS queries ქივორდებით
GOOGLE_NEWS_FEEDS = [
    f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=ka&gl=GE&ceid=GE:ka"
    for keyword in KEYWORDS
]

# პირდაპირი feed-ები, რაც ყველაზე მეტად მუშაობს
DIRECT_FEEDS = [
    "https://civil.ge/feed/",
    "https://netgazeti.ge/feed/",
    "https://publika.ge/feed/",
]

FEED_URLS = GOOGLE_NEWS_FEEDS + DIRECT_FEEDS


@dataclass
class Article:
    title: str
    link: str
    source: str
    published_dt: datetime
    published_text: str
    summary: str
    matched_keywords: List[str]
    uid: str


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_articles (
            uid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_entry_datetime(entry):
    if getattr(entry, "published_parsed", None):
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass

    if getattr(entry, "updated_parsed", None):
        try:
            return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass

    for field in ("published", "updated", "pubDate"):
        value = getattr(entry, field, None)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass

    return None


def is_within_last_hours(entry, hours: int) -> bool:
    entry_dt = parse_entry_datetime(entry)
    if entry_dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return entry_dt >= cutoff


def format_entry_datetime(entry) -> str:
    dt = parse_entry_datetime(entry)
    if not dt:
        return "უცნობი დრო"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def normalize_link(link: str) -> str:
    return (link or "").strip()


def make_uid(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}|{link}".encode("utf-8")).hexdigest()


def already_seen(conn: sqlite3.Connection, uid: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_articles WHERE uid = ?", (uid,))
    return cur.fetchone() is not None


def remember(conn: sqlite3.Connection, article: Article) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_articles (uid, title, link, created_at) VALUES (?, ?, ?, ?)",
        (article.uid, article.title, article.link, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def matched_keywords_in_text(text: str) -> List[str]:
    lower = text.casefold()
    return [kw for kw in KEYWORDS if kw.casefold() in lower]


def source_from_entry(entry, feed_url: str) -> str:
    source_title = strip_html(getattr(entry, "source", "")) if isinstance(getattr(entry, "source", None), str) else ""
    if source_title:
        return source_title

    if getattr(entry, "source", None):
        try:
            nested = strip_html(getattr(entry.source, "title", "") or "")
            if nested:
                return nested
        except Exception:
            pass

    feed = parse_feed(feed_url)
    if feed and getattr(feed, "feed", None):
        feed_title = strip_html(getattr(feed.feed, "title", "") or "")
        if feed_title:
            return feed_title

    try:
        return urlparse(feed_url).netloc.replace("www.", "")
    except Exception:
        return "უცნობი წყარო"


def parse_feed(url: str):
    try:
        feed = feedparser.parse(url)
        if getattr(feed, "bozo", 0) and not getattr(feed, "entries", None):
            print(f"Skipped bad feed: {url}")
            return None
        return feed
    except Exception as exc:
        print(f"Feed error for {url}: {exc}", file=sys.stderr)
        return None


def extract_entry_text(entry) -> str:
    title = strip_html(getattr(entry, "title", "") or "")
    summary = strip_html(
        getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    )
    return f"{title} {summary}".strip()


def shorten(text: str, width: int = 280) -> str:
    text = text.strip()
    if not text:
        return "მოკლე აღწერა არ არის მოცემული."
    return textwrap.shorten(text, width=width, placeholder="...")


def collect_articles(conn: sqlite3.Connection) -> List[Article]:
    items: dict[str, Article] = {}

    for feed_url in FEED_URLS:
        feed = parse_feed(feed_url)
        if not feed:
            continue

        for entry in feed.entries:
            if not is_within_last_hours(entry, BACKFILL_HOURS):
                continue

            title = strip_html(getattr(entry, "title", "") or "")
            summary = strip_html(
                getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
            )
            text_blob = extract_entry_text(entry)

            matches = matched_keywords_in_text(text_blob)
            if not matches:
                continue

            link = normalize_link(getattr(entry, "link", "") or "")
            if not link:
                continue

            uid = make_uid(title or "უსათაურო", link)
            if already_seen(conn, uid):
                continue

            published_dt = parse_entry_datetime(entry) or datetime.now(timezone.utc)

            article = Article(
                title=title or "უსათაურო მასალა",
                link=link,
                source=source_from_entry(entry, feed_url),
                published_dt=published_dt,
                published_text=format_entry_datetime(entry),
                summary=summary,
                matched_keywords=matches,
                uid=uid,
            )
            items[uid] = article

    articles = list(items.values())
    articles.sort(key=lambda a: a.published_dt, reverse=True)
    return articles[:MAX_POSTS_PER_RUN]


def build_message(article: Article) -> str:
    kws = ", ".join(article.matched_keywords)
    return (
        f"📰 <b>ახალი მასალა</b>\n\n"
        f"<b>სათაური:</b> {html.escape(article.title)}\n"
        f"<b>ქივორდი:</b> {html.escape(kws)}\n"
        f"<b>წყარო:</b> {html.escape(article.source)}\n"
        f"<b>დრო:</b> {html.escape(article.published_text)}\n\n"
        f"<b>მოკლე აღწერა:</b> {html.escape(shorten(article.summary))}\n\n"
        f'🔗 <a href="{html.escape(article.link)}">სტატიის ბმული</a>'
    )


def send_telegram_message(text: str) -> None:
    response = requests.post(
        TELEGRAM_API.format(token=BOT_TOKEN),
        data={
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def main() -> int:
    conn = init_db()
    articles = collect_articles(conn)

    if not articles:
        print("No new matching articles found.")
        return 0

    sent = 0
    for article in articles:
        try:
            send_telegram_message(build_message(article))
            remember(conn, article)
            sent += 1
            time.sleep(1)
        except Exception as exc:
            print(f"Failed to send article: {article.title}\nError: {exc}", file=sys.stderr)

    print(f"Sent {sent} article(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
