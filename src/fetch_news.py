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
    body: str = ""
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


# Google News wraps every link in an encoded redirect, but the entry summary
# still contains the publisher's own URL. That real URL gives better links in
# the email and, more importantly, often carries a date we can check.
PUBLISHER_LINK = re.compile(r'href="(https?://(?!news\.google\.)[^"]+)"')


def real_link(entry, fallback: str) -> str:
    if "news.google." not in fallback:
        return fallback
    match = PUBLISHER_LINK.search(getattr(entry, "summary", "") or "")
    return match.group(1) if match else fallback


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


# Most publishers put the publication date in the URL path. When a feed lies
# about its dates, or omits them, this is the more trustworthy signal.
URL_DATE = re.compile(r"/(20\d{2})/(\d{1,2})(?:/(\d{1,2}))?/")


def url_time(url: str) -> datetime | None:
    match = URL_DATE.search(url)
    if not match:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3) or "1"
    try:
        return datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
    except ValueError:
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
    stale = 0
    for entry in parsed.entries:
        link = real_link(entry, getattr(entry, "link", ""))
        title = clean_text(getattr(entry, "title", ""))
        if not link or not title:
            continue
        # An undated entry used to default to "now", which meant Google News
        # archive pages from years ago sailed straight into the brief. An item
        # we cannot date is an item we do not trust.
        published = entry_time(entry)
        if published is None:
            stale += 1
            continue

        # When the URL carries a date and the feed disagrees by more than a
        # week, believe the URL. Re-crawled archive pages are the usual cause.
        from_url = url_time(link)
        if from_url and abs((from_url - published).days) > 7:
            published = from_url

        now = datetime.now(timezone.utc)
        if published > now + timedelta(days=1) or published < cutoff:
            stale += 1
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
    log.info("%-38s %3d in window%s", name, len(stories),
             f", {stale} out of window or undated" if stale else "")
    return stories


PAGE_DATE = re.compile(
    r'(?:article:published_time"[^>]*content="|datePublished"\s*:\s*"'
    r'|itemprop="datePublished"[^>]*content="|name="pubdate"[^>]*content=")'
    r'(\d{4}-\d{2}-\d{2})', re.IGNORECASE)


SCRIPTS = re.compile(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>",
                     re.IGNORECASE | re.DOTALL)
ARTICLE = re.compile(r"<article[^>]*>(.*?)</article>", re.IGNORECASE | re.DOTALL)
PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def article_text(html: str, limit: int = 4000) -> str:
    """Pull readable body text out of a page.

    Deliberately crude. A real parser would be better, but paragraph text from
    inside <article> covers most publishers and never breaks the run.
    """
    html = SCRIPTS.sub(" ", html)
    scope = ARTICLE.search(html)
    region = scope.group(1) if scope else html
    paragraphs = [clean_text(p) for p in PARAGRAPH.findall(region)]
    paragraphs = [p for p in paragraphs if len(p) > 60]
    return " ".join(paragraphs)[:limit]


def fetch_page(url: str) -> tuple[datetime | None, str]:
    """Return the publisher's own publication date and the article text.

    Aggregators restamp old articles with a fresh crawl date, so the feed is
    not a trustworthy source of truth. The publisher's own page usually is,
    and while we are here the body text is far richer than a feed summary.
    """
    try:
        response = requests.get(url, timeout=12, allow_redirects=True,
                                headers={"User-Agent": USER_AGENT}, stream=True)
        raw = response.raw.read(400_000, decode_content=True).decode(
            "utf-8", errors="ignore")
        response.close()
    except Exception:  # noqa: BLE001
        return None, ""

    published = None
    match = PAGE_DATE.search(raw)
    if match:
        try:
            published = datetime.strptime(match.group(1), "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            published = None
    return published, article_text(raw)


def enrich(stories: list, max_age_days: int = 21) -> list:
    """Fetch each shortlisted story once: verify its date, keep its text.

    Fails open on the date. An unreachable page or one with no date metadata
    keeps the story, because dropping real news is worse than the occasional
    old one. Text is a bonus, and its absence only costs depth.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    kept, dropped, with_text = [], 0, 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda s: fetch_page(s.url), stories))
    for story, (actual, text) in zip(stories, results):
        if actual and actual < cutoff:
            log.info("dropped as stale (%s): %s", actual.date(), story.title[:70])
            dropped += 1
            continue
        if actual:
            story.published = actual
        if text:
            story.body = text
            with_text += 1
        kept.append(story)
    log.info("page check: %d kept, %d stale, %d with full text",
             len(kept), dropped, with_text)
    return kept


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
