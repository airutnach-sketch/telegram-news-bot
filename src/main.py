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
from typing import Iterable, List
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'seen.db')
GOOGLE_NEWS_RSS = 'https://news.google.com/rss/search?q={query}&hl={lang}&gl={country}&ceid={country}:{lang}'
TELEGRAM_API = 'https://api.telegram.org/bot{token}/sendMessage'
USER_AGENT = 'Mozilla/5.0 (compatible; TelegramNewsMonitor/1.0)'


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: str
    summary: str
    matched_keywords: List[str]
    uid: str


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return value or ''


BOT_TOKEN = env('TELEGRAM_BOT_TOKEN', required=True)
CHANNEL_ID = env('TELEGRAM_CHANNEL_ID', required=True)
KEYWORDS = [k.strip() for k in env('KEYWORDS', required=True).split(';') if k.strip()]
CHECK_WINDOW_DAYS = int(env('CHECK_WINDOW_DAYS', '7'))
MAX_POSTS_PER_RUN = int(env('MAX_POSTS_PER_RUN', '10'))
LANGUAGE = env('LANGUAGE', 'ka')
COUNTRY = env('COUNTRY', 'GE')


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS seen_articles (
            uid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        '''
    )
    conn.commit()
    return conn


def strip_html(raw: str) -> str:
    if not raw:
        return ''
    soup = BeautifulSoup(raw, 'html.parser')
    text = soup.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', html.unescape(text)).strip()


def parse_published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_recent(published: str) -> bool:
    dt = parse_published(published)
    if not dt:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(days=CHECK_WINDOW_DAYS)


def query_variants(keywords: Iterable[str]) -> List[str]:
    phrases = []
    for kw in keywords:
        if ' ' in kw:
            phrases.append(f'"{kw}"')
        else:
            phrases.append(kw)
    base = ' OR '.join(phrases)
    return [
        f'({base}) when:{CHECK_WINDOW_DAYS}d',
        f'({base}) Georgia politics when:{CHECK_WINDOW_DAYS}d',
        f'({base}) site:interpressnews.ge OR site:publika.ge OR site:tabula.ge OR site:netgazeti.ge when:{CHECK_WINDOW_DAYS}d',
    ]


def fetch_feed(query: str):
    url = GOOGLE_NEWS_RSS.format(query=quote_plus(query), lang=LANGUAGE, country=COUNTRY)
    return feedparser.parse(url)


def matched_keywords(text: str) -> List[str]:
    lower = text.casefold()
    return [kw for kw in KEYWORDS if kw.casefold() in lower]


def normalize_link(link: str) -> str:
    return link.strip()


def make_uid(title: str, link: str) -> str:
    return hashlib.sha256(f'{title}|{link}'.encode('utf-8')).hexdigest()


def already_seen(conn: sqlite3.Connection, uid: str) -> bool:
    cur = conn.execute('SELECT 1 FROM seen_articles WHERE uid = ?', (uid,))
    return cur.fetchone() is not None


def remember(conn: sqlite3.Connection, article: Article) -> None:
    conn.execute(
        'INSERT OR IGNORE INTO seen_articles (uid, title, link, created_at) VALUES (?, ?, ?, ?)',
        (article.uid, article.title, article.link, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def collect_articles(conn: sqlite3.Connection) -> List[Article]:
    items: dict[str, Article] = {}
    for query in query_variants(KEYWORDS):
        feed = fetch_feed(query)
        for entry in feed.entries:
            title = strip_html(getattr(entry, 'title', ''))
            summary = strip_html(getattr(entry, 'summary', ''))
            source = strip_html(getattr(entry, 'source', {}).get('title', '')) if getattr(entry, 'source', None) else ''
            published = getattr(entry, 'published', '') or getattr(entry, 'updated', '')
            text_blob = f'{title} {summary}'
            matches = matched_keywords(text_blob)
            if not matches:
                continue
            if not is_recent(published):
                continue
            link = normalize_link(getattr(entry, 'link', ''))
            uid = make_uid(title, link)
            if already_seen(conn, uid):
                continue
            article = Article(
                title=title,
                link=link,
                source=source or 'უცნობი წყარო',
                published=published or 'უცნობი დრო',
                summary=summary,
                matched_keywords=matches,
                uid=uid,
            )
            items[uid] = article
    articles = list(items.values())
    articles.sort(key=lambda a: parse_published(a.published) or datetime.now(timezone.utc), reverse=True)
    return articles[:MAX_POSTS_PER_RUN]


def shorten(text: str, width: int = 280) -> str:
    text = text.strip()
    if not text:
        return 'მოკლე აღწერა არ არის მოცემული.'
    return textwrap.shorten(text, width=width, placeholder='...')


def build_message(article: Article) -> str:
    kws = ', '.join(article.matched_keywords)
    return (
        f'📰 <b>ახალი მასალა</b>\n\n'
        f'<b>სათაური:</b> {html.escape(article.title)}\n'
        f'<b>ქივორდი:</b> {html.escape(kws)}\n'
        f'<b>წყარო:</b> {html.escape(article.source)}\n'
        f'<b>დრო:</b> {html.escape(article.published)}\n\n'
        f'<b>მოკლე აღწერა:</b> {html.escape(shorten(article.summary))}\n\n'
        f'🔗 <a href="{html.escape(article.link)}">სტატიის ბმული</a>'
    )


def send_telegram_message(text: str) -> None:
    response = requests.post(
        TELEGRAM_API.format(token=BOT_TOKEN),
        data={
            'chat_id': CHANNEL_ID,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False,
        },
        timeout=30,
        headers={'User-Agent': USER_AGENT},
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get('ok'):
        raise RuntimeError(f'Telegram API error: {payload}')


def main() -> int:
    conn = init_db()
    articles = collect_articles(conn)
    if not articles:
        print('No new matching articles found.')
        return 0

    sent = 0
    for article in articles:
        try:
            send_telegram_message(build_message(article))
            remember(conn, article)
            sent += 1
            time.sleep(1)
        except Exception as exc:
            print(f'Failed to send article: {article.title}\nError: {exc}', file=sys.stderr)

    print(f'Sent {sent} article(s).')
    return 0
import requests
import os

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHANNEL_ID")

url = f"https://api.telegram.org/bot{token}/sendMessage"

requests.post(url, data={
    "chat_id": chat_id,
    "text": "✅ TEST MESSAGE - ბოტი მუშაობს!"
})

import requests
import os
import time

if __name__ == "__main__":
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHANNEL_ID")

    msg = f"✅ BOT TEST {int(time.time())}"

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": msg},
        timeout=30,
    )

    print("STATUS:", response.status_code)
    print("BODY:", response.text)

    raise SystemExit(0)
