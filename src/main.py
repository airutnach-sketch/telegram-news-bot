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
USER_AGENT = "Mozilla/5.0 (compatible; TelegramNewsMonitor/3.0)"

# პირდაპირი feed-ები, რომლებიც ხშირად მუშაობს
DIRECT_FEEDS = [
    "https://civil.ge/feed/",
    "https://netgazeti.ge/feed/",
    "https://publika.ge/feed/",
]

# Google News RSS-ის პარამეტრები
GOOGLE_NEWS_HL = "ka"
GOOGLE_NEWS_GL = "GE"
GOOGLE_NEWS_CEID = "GE:ka"


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: str
    summary: str
    matched_keywords: List[str]
    uid: str
    published_dt: datetime | None


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


def google_news_feed_url(keyword: str) -> str:
    query = quote_plus(keyword)
    return (
        f"https://news.google.com/rss/search?q={query}"
        f"&hl={GOOGLE_NEWS_HL}&gl={GOOGLE_NEWS_GL}&ceid={GOOGLE_NEWS_CEID}"
    )


def all_feed_urls() -> List[str]:
    urls = list(DIRECT_FEEDS)
    for keyword in KEYWORDS:
        urls.append(google_news_feed_url(keyword))
    return list(dict.fromkeys(urls))


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


def entry_datetime_text(entry) -> str:
    dt = parse_entry_datetime(entry)
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


def tokenize_text(text: str) -> List[str]:
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def single_word_fuzzy_match(keyword_lower: str, words: List[str]) -> bool:
    # ზუსტი ან ქართული ბრუნვებით გაფართოებული დამთხვევა
    for word in words:
        if word == keyword_lower:
            return True
        if word.startswith(keyword_lower):
            return True
    return False


def phrase_fuzzy_match(keyword_lower: str, text_lower: str) -> bool:
    # ზუსტი ფრაზა
    if keyword_lower in text_lower:
        return True

    # მრავლისიტყვიანი ფრაზისთვის თითოეული სიტყვის არსებობა/ფორმა
    parts = [p for p in keyword_lower.split() if p]
    if not parts:
        return False

    words = tokenize_text(text_lower)
    for part in parts:
        matched_part = False
        for word in words:
            if word == part or word.startswith(part):
                matched_part = True
                break
        if not matched_part:
            return False
    return True


def matched_keywords(text: str) -> List[str]:
    text_lower = text.casefold()
    words = tokenize_text(text)
    matches: List[str] = []

    for kw in KEYWORDS:
        kw_lower = kw.casefold().strip()
        if not kw_lower:
            continue

        if " " in kw_lower:
            if phrase_fuzzy_match(kw_lower, text_lower):
                matches.append(kw)
        else:
            if single_word_fuzzy_match(kw_lower, words):
                matches.append(kw)

    # დუბლიკატების მოცილება, თან რიგის შენარჩუნება
    return list(dict.fromkeys(matches))


def looks_like_google_news(feed_url: str) -> bool:
    return "news.google.com" in feed_url


def source_from_entry(entry, feed_url: str, parsed_feed) -> str:
    if getattr(entry, "source", None):
        source_title = strip_html(getattr(entry.source, "title", "") or "")
        if source_title:
            return source_title

    if looks_like_google_news(feed_url):
        title = strip_html(getattr(entry, "title", "") or "")
        if " - " in title:
            parts = title.rsplit(" - ", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()

    feed_title = strip_html(getattr(parsed_feed.feed, "title", "") or "")
    if feed_title:
        return feed_title

    try:
        return urlparse(feed_url).netloc.replace("www.", "")
    except Exception:
        return "უცნობი წყარო"


def cleaned_title(entry, feed_url: str) -> str:
    title = strip_html(getattr(entry, "title", "") or "")
    if looks_like_google_news(feed_url) and " - " in title:
        parts = title.rsplit(" - ", 1)
        if len(parts) == 2 and parts[0].strip():
            return parts[0].strip()
    return title


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

    for feed_url in all_feed_urls():
        feed = parse_feed(feed_url)
        if not feed:
            continue

        for entry in feed.entries:
            if not is_within_last_hours(entry, BACKFILL_HOURS):
                continue

            title = cleaned_title(entry, feed_url)
            summary = strip_html(
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
                or ""
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
                source=source_from_entry(entry, feed_url, feed),
                published=entry_datetime_text(entry),
                summary=summary,
                matched_keywords=matches,
                uid=uid,
                published_dt=published_dt,
            )
            items[uid] = article

    articles = list(items.values())
    articles.sort(
        key=lambda a: a.published_dt or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return articles[:MAX_POSTS_PER_RUN]


def shorten(text: str, width: int = 320) -> str:
    text = text.strip()
    if not text:
        return "მოკლე აღწერა არ არის მოცემული."
    return textwrap.shorten(text, width=width, placeholder="...")


def build_message(article: Article) -> str:
    kws = ", ".join(article.matched_keywords)
    return (
        f"📰 <b>ახალი მასალა</b>\n\n"
        f"<b>სათაური:</b> {html.escape(article.title)}\n"
        f"<b>ქივორდები:</b> {html.escape(kws)}\n"
        f"<b>წყარო:</b> {html.escape(article.source)}\n"
        f"<b>დრო:</b> {html.escape(article.published)}\n\n"
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
