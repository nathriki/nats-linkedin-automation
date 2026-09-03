"""Kill-switch: pauses auto-posting after repeated risky-post incidents.

There is no automated way to detect, after the fact, that a published post
was actually risky (the classifier already said "safe" -- that's exactly
the false-negative scenario this exists for). So the trigger is a human
flag (see the approval bot's /flag command): the user notices something
went wrong and flags the news_id. 2+ flags within a rolling 24h window
pauses auto-posting (design doc Implementation Task T5) until /unpause.

Uses the same NATS KV pattern as every other stage's dedup/state store.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from nats.js.errors import BucketNotFoundError, KeyNotFoundError

KILL_SWITCH_BUCKET = "kill_switch"
FLAGS_KEY = "flag_timestamps"
CONTROL_BUCKET = "pipeline_control"
PAUSED_KEY = "poster_paused"

FLAG_THRESHOLD = 2
FLAG_WINDOW_HOURS = 24.0


async def _get_or_create_kv(js, bucket: str):
    try:
        return await js.key_value(bucket)
    except BucketNotFoundError:
        return await js.create_key_value(bucket=bucket)


def count_recent_flags(flags: list[dict], now: datetime, window_hours: float = FLAG_WINDOW_HOURS) -> int:
    """Pure function: how many flag records fall within the trailing window."""
    cutoff = now - timedelta(hours=window_hours)
    count = 0
    for flag in flags:
        ts = datetime.fromisoformat(flag["at"])
        if ts >= cutoff:
            count += 1
    return count


async def _load_flags(kv) -> list[dict]:
    try:
        entry = await kv.get(FLAGS_KEY)
        return json.loads(entry.value.decode())
    except KeyNotFoundError:
        return []


async def _save_flags(kv, flags: list[dict]) -> None:
    await kv.put(FLAGS_KEY, json.dumps(flags).encode())


async def record_flag(js, news_id: str, reason: str, at: Optional[datetime] = None) -> bool:
    """Records a flag against a news_id. Returns True if this flag pushed the
    rolling count over FLAG_THRESHOLD, which also pauses auto-posting."""
    at = at or datetime.now(timezone.utc)
    kv = await _get_or_create_kv(js, KILL_SWITCH_BUCKET)
    flags = await _load_flags(kv)
    flags.append({"news_id": news_id, "reason": reason, "at": at.isoformat()})
    await _save_flags(kv, flags)

    triggered = count_recent_flags(flags, at) >= FLAG_THRESHOLD
    if triggered:
        await set_paused(js, True)
    return triggered


async def set_paused(js, paused: bool) -> None:
    kv = await _get_or_create_kv(js, CONTROL_BUCKET)
    await kv.put(PAUSED_KEY, b"1" if paused else b"0")


async def is_paused(js) -> bool:
    kv = await _get_or_create_kv(js, CONTROL_BUCKET)
    try:
        entry = await kv.get(PAUSED_KEY)
        return entry.value == b"1"
    except KeyNotFoundError:
        return False
