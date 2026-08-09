"""Pull candidate stories from RSS feeds and Google News queries."""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml

log = logging.getLogger(__name__)

# Several trade sites and Google News reject non browser agents outright.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 pc-ai-brief/1.0")
TIMEOUT = 20
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                   "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid"}


@dataclass
class Story:
    title: str
    url: str
    source: str
    region: str
    source_weight: float
    published: datetime
    summary: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "region": self.region,
            "published": self.published.isoformat(),
            "summary": self.summary[:600],
            "score": round(self.score, 2),
            "reasons": self.reasons,
        }


def load_sources(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
                 if k not in TRACKING_PARAMS]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path.rstrip("/"),
             urllib.parse.urlencode(query), "")
        )
    except ValueError:
        return url


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(name: str, url: str, weight: float, region: str,
               window_hours: int) -> list[Story]:
    """Fetch one feed. Never raises: a broken source must not kill the run."""
    stories: list[Story] = []
    try:
        response = requests.get(url, timeout=TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("source unavailable: %s (%s)", name, exc)
        return stories

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    for entry in parsed.entries:
        link = getattr(entry, "link", "")
        title = clean_text(getattr(entry, "title", ""))
        if not link or not title:
            continue
        published = entry_time(entry) or datetime.now(timezone.utc)
        if published < cutoff:
            continue
        stories.append(Story(
            title=title,
            url=canonical_url(link),
            source=clean_text(getattr(entry, "source", {}).get("title", "")) or name,
            region=region,
            source_weight=weight,
            published=published,
            summary=clean_text(getattr(entry, "summary", ""))[:1200],
        ))
    log.info("%-38s %3d in window", name, len(stories))
    return stories


def google_news_url(query: str, edition: str) -> str:
    return (f"https://news.google.com/rss/search?q="
            f"{urllib.parse.quote_plus(query)}&{edition}")


def collect(config: dict, window_hours: int = 30) -> list[Story]:
    jobs = []
    for feed in config.get("feeds", []):
        jobs.append((feed["name"], feed["url"], float(feed.get("weight", 1.0)),
                     feed.get("region", "Global")))

    gn = config.get("google_news") or {}
    edition = gn.get("edition", "hl=en-CA&gl=CA&ceid=CA:en")
    gn_weight = float(gn.get("weight", 1.5))
    for query in gn.get("queries", []):
        jobs.append((f"Google News: {query[:40]}", google_news_url(query, edition),
                     gn_weight, "Global"))

    stories: list[Story] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_feed, name, url, weight, region, window_hours)
                   for name, url, weight, region in jobs]
        for future in as_completed(futures):
            stories.extend(future.result())

    log.info("collected %d raw stories from %d sources", len(stories), len(jobs))
    return stories
