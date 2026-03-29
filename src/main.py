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
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "seen.db")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
USER_AGENT = "Mozilla/5.0 (compatible; TelegramNewsMonitor/1.0)"

FEED_URLS = [
    "https://civil.ge/feed/",
    "https://netgazeti.ge/feed/",
    "https://www.interpressnews.ge/ge/index.php/feed/index.1.rss",
    "https://publika.ge/feed/",
    "https://agenda.ge/feed/",
    "https://imedi.ge/feed/",
    "https://imedinews.ge/feed/",
    "https://tabula.ge/feed/",
    "https://timer.ge/feed/",
    "https://tvpirveli.ge/feed/",
    "https://2020news.ge/feed/",
    "https://metronome.ge/feed/",
    "https://news.ge/feed/",
]


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: datetime
    summary: str
    matched_keywords: List[str]
    uid: str


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def parse_keywords(raw: str) -> List[str]:
    parts = re.split(r"[,\n;]+", raw)
    return [p.strip() for p in parts if p.strip()]


BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", required=True)
CHANNEL_ID = env("TELEGRAM_CHANNEL_ID", required=True)
KEYWORDS = parse_keywords(env("KEYWORDS", required=True))
BACKFILL_HOURS = int(env("BACKFILL_HOURS", "24"))
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "25"))


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


def parse_entry_datetime(entry) -> datetime | None:
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


def entry_datetime_text(dt: datetime | None) -> str:
    if not dt:
        return "უცნობი დრო"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def normalize_link(link: str) -> str:
    return link.strip()


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


def matched_keywords(text: str) -> List[str]:
    lower = text.casefold()
    return [kw for kw in KEYWORDS if kw.casefold() in lower]


def source_from_entry(feed_url: str) -> str:
    try:
        parsed = urlparse(feed_url)
        return parsed.netloc.replace("www.", "")
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
            text_blob = f"{title} {summary}"

            matches = matched_keywords(text_blob)
            if not matches:
                continue

            link = normalize_link(getattr(entry, "link", "") or "")
            if not link:
                continue

            uid = make_uid(title, link)
            if already_seen(conn, uid):
                continue

            published_dt = parse_entry_datetime(entry)

            article = Article(
                title=title or "უსათაურო მასალა",
                link=link,
                source=source_from_entry(feed_url),
                published=published_dt or datetime.now(timezone.utc),
                summary=summary,
                matched_keywords=matches,
                uid=uid,
            )
            items[uid] = article

    articles = list(items.values())
    articles.sort(key=lambda a: a.published, reverse=True)
    return articles[:MAX_POSTS_PER_RUN]


def shorten(text: str, width: int = 280) -> str:
    text = text.strip()
    if not text:
        return "მოკლე აღწერა არ არის მოცემული."
    return textwrap.shorten(text, width=width, placeholder="...")


def build_message(article: Article) -> str:
    kws = ", ".join(article.matched_keywords)
    return (
        f"📰 <b>ახალი მასალა</b>\n\n"
        f"<b>სათაური:</b> {html.escape(article.title)}\n"
        f"<b>ქივორდი:</b> {html.escape(kws)}\n"
        f"<b>წყარო:</b> {html.escape(article.source)}\n"
        f"<b>დრო:</b> {html.escape(entry_datetime_text(article.published))}\n\n"
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
            print(
                f"Failed to send article: {article.title}\nError: {exc}",
                file=sys.stderr,
            )

    print(f"Sent {sent} article(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
