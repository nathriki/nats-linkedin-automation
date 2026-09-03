"""Dedup-key gate for the poster (design doc Implementation Task T4).

Closes the JetStream at-least-once double-post risk: before posting to
LinkedIn, the poster checks whether this news_id has already been posted.
Built now (ahead of the actual LinkedIn-calling poster in Next Step 10) so
the gate exists and is tested independently of the LinkedIn API integration.

Same NATS KV pattern as the scraper's seen_news and the drafter's
drafter_state buckets.
"""
from datetime import datetime, timezone
from typing import Optional

from nats.js.errors import BucketNotFoundError, KeyNotFoundError

POSTED_BUCKET = "posted_news"


async def get_or_create_kv(js):
    try:
        return await js.key_value(POSTED_BUCKET)
    except BucketNotFoundError:
        return await js.create_key_value(bucket=POSTED_BUCKET)


async def already_posted(kv, news_id: str) -> bool:
    try:
        await kv.get(news_id)
        return True
    except KeyNotFoundError:
        return False


async def mark_posted(kv, news_id: str, at: Optional[datetime] = None) -> None:
    at = at or datetime.now(timezone.utc)
    await kv.put(news_id, at.isoformat().encode())
