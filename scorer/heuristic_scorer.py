"""Scorer service: news.raw -> news.scored.

Cheap heuristic only -- no LLM call here, deliberately. Keyword relevance
(is this actually about PH corporate/office worker issues, per Premise 1)
combined with a recency decay (more "trending" = more recent), then only
the top 1-3 candidates get published onward. This is what keeps LLM spend
bounded regardless of how many items the scraper happens to find (see the
design doc's performance constraint) -- the drafter and risk-classifier,
which DO call an LLM, only ever see the survivors of this filter.

Run: .venv/Scripts/python.exe scorer/heuristic_scorer.py
"""
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import nats
from nats.errors import TimeoutError as NatsTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import NEWS_RAW, NEWS_SCORED, NewsRaw, NewsScored  # noqa: E402

NATS_URL = "nats://localhost:4222"
CONSUMER_DURABLE_NAME = "scorer-news-raw"
FETCH_BATCH_SIZE = 100
FETCH_TIMEOUT_SECONDS = 5

TOP_N = 3
RECENCY_FLOOR = 0.2
RECENCY_HALFLIFE_HOURS = 48.0

# Cheap keyword heuristic for "is this actually about PH corporate/office
# worker issues" (Premise 1's content scope). Not exhaustive by design --
# this is meant to be a fast filter, refined later against real results,
# not a precise classifier (that's the LLM-based risk-classifier's job,
# downstream, on the survivors of this filter).
KEYWORDS: dict[str, float] = {
    # high-weight: directly on-topic
    "wage": 3.0,
    "wages": 3.0,
    "minimum wage": 3.0,
    "dole": 3.0,
    "contractualization": 3.0,
    "contractual worker": 3.0,
    "endo": 3.0,  # PH shorthand for "end of contract" hiring practice
    "bpo": 3.0,
    "return to office": 3.0,
    "rto": 3.0,
    "labor group": 3.0,
    "labor union": 3.0,
    "trade union": 3.0,
    "collective bargaining": 3.0,
    "layoff": 3.0,
    "layoffs": 3.0,
    "retrenchment": 3.0,
    "night shift differential": 3.0,
    "13th month": 3.0,
    "overtime pay": 3.0,
    "work from home": 3.0,
    "wfh": 3.0,
    "hybrid work": 3.0,
    "remote work": 3.0,
    "regularization": 3.0,
    "security of tenure": 3.0,
    "unemployment rate": 3.0,
    "labor secretary": 3.0,
    # medium-weight: adjacent/context terms, on their own not enough to
    # prove relevance but reinforce a high-weight match
    "worker": 1.0,
    "workers": 1.0,
    "employee": 1.0,
    "employees": 1.0,
    "corporate": 1.0,
    "employment": 1.0,
    "workforce": 1.0,
    "salary": 1.0,
    "benefits": 1.0,
}

# \b word-boundary matching, not plain substring -- otherwise "rto" matches
# inside "Alberto" and "dole" matches inside "condole", etc.
_COMPILED_KEYWORDS = [
    (term, weight, re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE))
    for term, weight in KEYWORDS.items()
]


def _keyword_score(text: str) -> tuple[float, list[str]]:
    total = 0.0
    matched = []
    for term, weight, pattern in _COMPILED_KEYWORDS:
        if pattern.search(text):
            total += weight
            matched.append(term)
    return total, matched


def _recency_factor(published_at: datetime, now: datetime) -> float:
    hours_old = max(0.0, (now - published_at).total_seconds() / 3600.0)
    factor = 1.0 - (hours_old / RECENCY_HALFLIFE_HOURS)
    return max(RECENCY_FLOOR, factor)


def score_candidate(item: NewsRaw, now: Optional[datetime] = None) -> tuple[float, str]:
    """Pure function: NewsRaw -> (score, human-readable reason).

    Score of 0.0 means "no relevant keywords at all" -- these are excluded
    regardless of recency; a very recent story about the weather is still
    not a corporate-worker story.
    """
    now = now or datetime.now(timezone.utc)
    text = f"{item.title} {item.excerpt}".lower()
    keyword_total, matched_terms = _keyword_score(text)

    if keyword_total == 0.0:
        return 0.0, "no relevant keywords matched"

    recency = _recency_factor(item.published_at, now)
    score = keyword_total * recency
    reason = (
        f"keywords=[{', '.join(sorted(set(matched_terms)))}] "
        f"keyword_score={keyword_total:.1f} recency_factor={recency:.2f}"
    )
    return score, reason


async def run_once() -> None:
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    sub = await js.pull_subscribe(NEWS_RAW, durable=CONSUMER_DURABLE_NAME, stream="NEWS")

    try:
        msgs = await sub.fetch(FETCH_BATCH_SIZE, timeout=FETCH_TIMEOUT_SECONDS)
    except NatsTimeoutError:
        msgs = []

    now = datetime.now(timezone.utc)
    candidates = []
    for msg in msgs:
        item = NewsRaw.model_validate_json(msg.data)
        score, reason = score_candidate(item, now)
        candidates.append((item, msg, score, reason))

    relevant = [c for c in candidates if c[2] > 0.0]
    relevant.sort(key=lambda c: c[2], reverse=True)
    top = relevant[:TOP_N]

    for item, _msg, score, reason in top:
        scored = NewsScored(**item.model_dump(), score=score, score_reason=reason)
        await js.publish(NEWS_SCORED, scored.model_dump_json().encode())

    # Ack every fetched message, selected or not -- it's been considered,
    # and the scorer should never re-score the same news.raw item twice.
    for _item, msg, _score, _reason in candidates:
        await msg.ack()

    print(f"Considered {len(candidates)} candidates, {len(relevant)} relevant, published top {len(top)} to news.scored")
    for item, _msg, score, reason in top:
        print(f"  -> {item.title!r} score={score:.2f} ({reason})")
    if not candidates:
        print("No pending news.raw items (scraper hasn't run, or scorer already caught up).")

    await nc.close()


if __name__ == "__main__":
    asyncio.run(run_once())
