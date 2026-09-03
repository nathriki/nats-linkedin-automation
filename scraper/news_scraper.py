"""Scraper service: Philstar + GMA News + Inquirer RSS -> news.raw / news.raw.failed.

Consumes ONLY the RSS feed itself (title, short <description> excerpt,
link, pubDate, author, guid) -- never fetches the linked article page. See
the design doc's Constraints for why (copyright/ToS discipline, same
reasoning as the libel-safety rules).

Each source was checked against its own robots.txt and terms before being
added here, same discipline as Philstar's original Next Step 2:
  - Philstar: RSS explicitly offered for this kind of use.
  - GMA News: robots.txt explicitly allows AI crawlers (ClaudeBot,
    anthropic-ai included by name), and the feed's own copyright notice
    explicitly permits "excerpts and links... with appropriate and
    specific direction to the original content" -- exactly this pipeline's
    pattern.
  - Inquirer: their User Agreement restricts external reuse to headlines
    only without separate syndication clearance, so INQUIRER_FEEDS is
    marked include_excerpt=False -- the scraper always publishes excerpt=""
    for this source, never the RSS <description> text.
  - Rappler was considered and deliberately excluded: its robots.txt
    disallows "anthropic-ai" and "ClaudeBot" by name, a direct signal that
    this kind of AI use isn't welcome there.

Meant to be invoked periodically by an external scheduler (cron / Task
Scheduler) -- see the design doc's "not yet decided" note on scheduling.
Each run is a single pass over every feed, then exits.

Run: .venv/Scripts/python.exe scraper/news_scraper.py
"""
import asyncio
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import nats
import requests
from nats.js.errors import BucketNotFoundError, KeyNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import NEWS_RAW, NEWS_RAW_FAILED, NewsRaw, NewsRawFailed, compute_news_id  # noqa: E402

NATS_URL = "nats://localhost:4222"
SEEN_BUCKET = "seen_news"

# (source, feed_name, url, include_excerpt) -- feed_name is a canonical
# "nation" | "business" category, independent of each source's own section
# naming (e.g. GMA's "money/economy" section maps to our "business").
FEEDS = [
    ("philstar", "nation", "https://www.philstar.com/rss/nation", True),
    ("philstar", "business", "https://www.philstar.com/rss/business", True),
    ("gma", "nation", "https://data.gmanetwork.com/gno/rss/news/nation/feed.xml", True),
    ("gma", "business", "https://data.gmanetwork.com/gno/rss/money/economy/feed.xml", True),
    ("inquirer", "nation", "https://newsinfo.inquirer.net/feed", False),
    ("inquirer", "business", "https://business.inquirer.net/feed", False),
]

HEADERS = {
    "User-Agent": "nats-linkedin-automation-scraper/0.1 (personal project, PH corporate-worker news pipeline)"
}

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 15

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class FeedFetchError(Exception):
    """Raised when a feed could not be fetched after all retries."""


def fetch_feed_with_retry(url: str) -> str:
    backoff = INITIAL_BACKOFF_SECONDS
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
    raise FeedFetchError(f"Failed to fetch {url} after {MAX_RETRIES} attempts: {last_exc}") from last_exc


def _text(item_el, tag: str) -> Optional[str]:
    el = item_el.find(tag)
    if el is None or el.text is None:
        return None
    stripped = el.text.strip()
    return stripped or None


def _clean_excerpt(raw: Optional[str]) -> str:
    """GMA's <description> embeds an <img> tag + HTML around the text
    (unlike Philstar's plain text); strip markup so the drafter/scorer
    always see clean prose regardless of source."""
    if not raw:
        return ""
    return _HTML_TAG_RE.sub("", raw).strip()


def _parse_pub_date(raw: Optional[str], fallback: datetime) -> datetime:
    if not raw:
        return fallback
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed_xml(
    xml_text: str,
    source: str,
    feed_name: str,
    include_excerpt: bool,
    scraped_at: Optional[datetime] = None,
) -> list[NewsRaw]:
    """Pure function: RSS XML -> list of NewsRaw. No network, no NATS --
    kept separate from fetch/publish so it's testable with a canned string."""
    scraped_at = scraped_at or datetime.now(timezone.utc)
    # Inquirer's business feed (at least) sends stray whitespace before the
    # XML declaration, which ElementTree rejects outright since the
    # declaration must be the very first thing in the document.
    root = ET.fromstring(xml_text.lstrip())
    items: list[NewsRaw] = []

    for item_el in root.findall("./channel/item"):
        title = _text(item_el, "title")
        link = _text(item_el, "link")
        if not title or not link:
            continue  # skip a malformed item rather than fail the whole feed

        excerpt = _clean_excerpt(_text(item_el, "description")) if include_excerpt else ""

        items.append(
            NewsRaw(
                news_id=compute_news_id(link),
                source=source,
                feed=feed_name,
                title=title,
                link=link,
                excerpt=excerpt,
                author=_text(item_el, "author"),
                guid=_text(item_el, "guid") or link,
                published_at=_parse_pub_date(_text(item_el, "pubDate"), scraped_at),
                scraped_at=scraped_at,
            )
        )
    return items


async def _get_or_create_kv(js):
    try:
        return await js.key_value(SEEN_BUCKET)
    except BucketNotFoundError:
        return await js.create_key_value(bucket=SEEN_BUCKET)


async def _already_seen(kv, news_id: str) -> bool:
    try:
        await kv.get(news_id)
        return True
    except KeyNotFoundError:
        return False


async def run_once() -> None:
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    kv = await _get_or_create_kv(js)

    published = 0
    skipped_seen = 0
    failed_feeds = 0

    for source, feed_name, url, include_excerpt in FEEDS:
        try:
            xml_text = fetch_feed_with_retry(url)
        except FeedFetchError as exc:
            failed_feeds += 1
            failure = NewsRawFailed(
                source=source,
                feed=feed_name,
                error=str(exc),
                occurred_at=datetime.now(timezone.utc),
                retry_count=MAX_RETRIES,
            )
            await js.publish(NEWS_RAW_FAILED, failure.model_dump_json().encode())
            print(f"[{source}/{feed_name}] FAILED after {MAX_RETRIES} retries: {exc}")
            continue

        try:
            items = parse_feed_xml(xml_text, source, feed_name, include_excerpt)
        except ET.ParseError as exc:
            # A malformed feed from one source must never take down the
            # rest of the run -- same "each source is independent" logic
            # already applied to fetch failures above.
            failed_feeds += 1
            failure = NewsRawFailed(
                source=source,
                feed=feed_name,
                error=f"XML parse error: {exc}",
                occurred_at=datetime.now(timezone.utc),
                retry_count=0,
            )
            await js.publish(NEWS_RAW_FAILED, failure.model_dump_json().encode())
            print(f"[{source}/{feed_name}] FAILED to parse: {exc}")
            continue

        new_in_feed = 0
        for item in items:
            if await _already_seen(kv, item.news_id):
                skipped_seen += 1
                continue
            await js.publish(NEWS_RAW, item.model_dump_json().encode())
            await kv.put(item.news_id, item.scraped_at.isoformat().encode())
            published += 1
            new_in_feed += 1

        print(f"[{source}/{feed_name}] fetched {len(items)} items, {new_in_feed} new")

    if published == 0 and failed_feeds == 0:
        print("No new items in any feed this run (not a failure -- genuine zero-news pass).")

    print(f"Done. published={published} skipped_already_seen={skipped_seen} failed_feeds={failed_feeds}")
    await nc.close()


if __name__ == "__main__":
    asyncio.run(run_once())
