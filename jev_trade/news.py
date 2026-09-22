"""Headline collection from publisher-provided RSS feeds (no scraping, no unofficial endpoints).

Every source below publishes its feed for syndication, so there is no blocking
risk of the kind an unofficial search endpoint carries. Each headline is
{id, title, source, published_utc, link, feed}. `NewsStore` remembers which
titles the monitor has already evaluated so each check only sends new items to Jev.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests

# name -> (url, category). Verified reachable on 2026-09-22.
FEEDS: dict[str, tuple[str, str]] = {
    "coindesk": ("https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
    "cointelegraph": ("https://cointelegraph.com/rss", "crypto"),
    "theblock": ("https://www.theblock.co/rss.xml", "crypto"),
    "bitcoinmagazine": ("https://bitcoinmagazine.com/.rss/full/", "crypto"),
    "cnbc_top": ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "markets"),
    "cnbc_finance": ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "markets"),
    "cnbc_economy": ("https://www.cnbc.com/id/20910258/device/rss/rss.html", "economy"),
    "marketwatch_top": ("https://feeds.content.dowjones.io/public/rss/mw_topstories", "markets"),
    "yahoo_finance": ("https://finance.yahoo.com/news/rssindex", "markets"),
    "fed_press": ("https://www.federalreserve.gov/feeds/press_all.xml", "government"),
    "sec_press": ("https://www.sec.gov/news/pressreleases.rss", "government"),
    "bls": ("https://www.bls.gov/feed/bls_latest.rss", "government"),
}
DEFAULT_FEEDS = list(FEEDS.keys())
SOURCE_LABELS = {
    "coindesk": "CoinDesk", "cointelegraph": "Cointelegraph", "theblock": "The Block",
    "bitcoinmagazine": "Bitcoin Magazine", "cnbc_top": "CNBC", "cnbc_finance": "CNBC Finance",
    "cnbc_economy": "CNBC Economy", "marketwatch_top": "MarketWatch", "yahoo_finance": "Yahoo Finance",
    "fed_press": "Federal Reserve press release", "sec_press": "SEC press release", "bls": "US BLS release",
}

_UA = {"User-Agent": "jev_trade/0.1 (RSS reader; contact via repository)"}


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip()


def _key(title: str) -> str:
    return hashlib.sha1(title.lower().encode("utf-8")).hexdigest()[:16]


def _parse_date(entry) -> datetime | None:
    for field in ("published", "updated"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
    for field in ("published_parsed", "updated_parsed"):
        st = entry.get(field)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_feed(name: str, url: str, timeout: float = 15.0) -> list[dict]:
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code != 200:
            return []
        feed = feedparser.parse(r.content)
    except Exception:
        return []
    items = []
    for e in feed.entries:
        title = _clean_title(e.get("title", ""))
        if not title:
            continue
        published = _parse_date(e)
        source = SOURCE_LABELS.get(name) or (feed.feed.get("title") or name).strip()
        items.append(
            {
                "id": _key(title),
                "title": title,
                "source": source[:40],
                "published_utc": published.strftime("%Y-%m-%d %H:%M") if published else None,
                "_ts": published.timestamp() if published else 0.0,
                "link": e.get("link"),
                "feed": name,
            }
        )
    return items


def fetch_headlines(feeds: list[str] | None = None, max_age_hours: float = 36.0, per_feed: int = 30) -> list[dict]:
    """Fetch, de-duplicate across feeds, drop stale items, newest first.

    `feeds` may contain names from FEEDS or raw RSS URLs.
    """
    feeds = feeds or DEFAULT_FEEDS
    cutoff = time.time() - max_age_hours * 3600
    seen: dict[str, dict] = {}
    for f in feeds:
        url = FEEDS[f][0] if f in FEEDS else f
        name = f if f in FEEDS else re.sub(r"^https?://(www\.)?", "", f).split("/")[0]
        for item in fetch_feed(name, url)[:per_feed]:
            if item["_ts"] and item["_ts"] < cutoff:
                continue
            if item["id"] in seen:
                continue
            seen[item["id"]] = item
    return sorted(seen.values(), key=lambda x: x["_ts"], reverse=True)


def for_llm(items: list[dict], limit: int = 60) -> list[dict]:
    return [{"published_utc": i["published_utc"], "source": i["source"], "title": i["title"]} for i in items[:limit]]


class NewsStore:
    """Remembers headline ids already evaluated by the monitor."""

    def __init__(self, path: Path, max_ids: int = 3000):
        self.path = path
        self.max_ids = max_ids
        self.seen: dict[str, float] = {}
        if path.exists():
            try:
                self.seen = json.loads(path.read_text())
            except Exception:
                self.seen = {}

    def new_only(self, items: list[dict]) -> list[dict]:
        return [i for i in items if i["id"] not in self.seen]

    def mark(self, items: list[dict]) -> None:
        now = time.time()
        for i in items:
            self.seen[i["id"]] = now
        if len(self.seen) > self.max_ids:
            keep = sorted(self.seen.items(), key=lambda kv: kv[1], reverse=True)[: self.max_ids]
            self.seen = dict(keep)
        self.path.write_text(json.dumps(self.seen))


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
