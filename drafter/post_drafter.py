"""Drafter service: news.scored -> post.drafted.

Uses Gemini (gemini-3.5-flash, free tier) via a Pydantic response_schema --
this IS the "structured output field" the design doc calls for (outside
voice finding 5): the disclosure line is a separate, required,
schema-validated field, not something we're hoping the model remembered to
include in free-form prose.

Two rules baked into the system prompt per the Next Step 0a/0b findings:
  (a) worker-first framing -- tell the composite worker's experience, only
      name a specific official/company when essential to the point.
  (b) varied disclosure phrasing -- never repeat a previous disclosure line
      verbatim, or the disclosure itself becomes a templated, bot-detectable
      tell (the exact failure mode LinkedIn's algorithm penalizes).

Run: .venv/Scripts/python.exe drafter/post_drafter.py
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import nats
from dotenv import load_dotenv
from google import genai
from google.genai import types
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import BucketNotFoundError, KeyNotFoundError
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import NEWS_SCORED, POST_DRAFTED, NewsScored, PostDrafted  # noqa: E402

load_dotenv()

NATS_URL = "nats://localhost:4222"
CONSUMER_DURABLE_NAME = "drafter-news-scored"
FETCH_BATCH_SIZE = 10
FETCH_TIMEOUT_SECONDS = 5

DRAFTER_STATE_BUCKET = "drafter_state"
RECENT_DISCLOSURES_KEY = "recent_disclosures"
MAX_RECENT_DISCLOSURES = 10

MODEL = "gemini-3.5-flash-lite"  # free tier -- switched from gemini-3.5-flash after its free tier turned out to be capped at 20 requests/DAY (not just per-minute); lite tier trades some quality for a much higher daily quota
MAX_DUPLICATE_RETRIES = 1

SYSTEM_PROMPT = """You write short LinkedIn posts for Filipino corporate and BPO office workers, based on real Philippine news about labor policy, wages, RTO mandates, contractualization, and other workplace issues.

Non-negotiable rules:
1. Every post is told through a composite, fictional worker archetype (invent a first name only, e.g. "Jana", "Mark", "Diane", "Rico") -- never claim to describe one real, identifiable person's actual account. The archetype is a storytelling device built from the general pattern in the news, not a specific individual.
2. The post must naturally disclose that this is a composite/fictional framing -- not a legal-disclaimer-style opener, but a natural line early in the post (e.g. "Meet 'Jana' -- a composite drawn from real BPO worker stories, not one real person."). Vary this disclosure's exact wording every time; never reuse a previous phrasing.
3. Default to telling the WORKER's experience and feelings, not the political official's or company's action. Only name a specific official or company if the post is meaningless without it -- most posts should need no specific name at all.
4. Ground every factual claim in the provided news item (title + excerpt) -- never invent facts beyond what's given.
5. Write like a real person on LinkedIn: short sentences, concrete details, no corporate voice. Never use AI-cliche vocabulary (delve, crucial, robust, comprehensive, furthermore, moreover, tapestry, landscape, foster, showcase, intricate, vibrant, significant). Roughly 80-160 words.
6. Output through the structured fields only. draft_text is the full post, disclosure line included naturally within it. archetype_disclosure is an exact copy of just that one disclosure sentence from draft_text."""


class DraftOutput(BaseModel):
    draft_text: str
    archetype_disclosure: str


def _normalize_disclosure(text: str) -> str:
    """Lowercase + strip punctuation/whitespace, for exact-duplicate
    comparison. Not fuzzy matching -- a cheap heuristic, consistent with
    the rest of this pipeline's "cheap heuristic first" philosophy."""
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def is_duplicate_disclosure(candidate: str, recent: list[str]) -> bool:
    normalized_candidate = _normalize_disclosure(candidate)
    return any(normalized_candidate == _normalize_disclosure(r) for r in recent)


def build_user_prompt(item: NewsScored, recent_disclosures: list[str], retry_note: Optional[str] = None) -> str:
    disclosures_block = "\n".join(f"- {d}" for d in recent_disclosures) or "(none yet)"
    prompt = f"""News item:
Title: {item.title}
Excerpt: {item.excerpt}
Source: {item.link}

Recent disclosure lines already used -- do not reuse any of this exact phrasing:
{disclosures_block}

Write the post now."""
    if retry_note:
        prompt += f"\n\n{retry_note}"
    return prompt


async def _get_or_create_kv(js):
    try:
        return await js.key_value(DRAFTER_STATE_BUCKET)
    except BucketNotFoundError:
        return await js.create_key_value(bucket=DRAFTER_STATE_BUCKET)


async def _load_recent_disclosures(kv) -> list[str]:
    try:
        entry = await kv.get(RECENT_DISCLOSURES_KEY)
        return json.loads(entry.value.decode())
    except KeyNotFoundError:
        return []


async def _save_recent_disclosures(kv, disclosures: list[str]) -> None:
    trimmed = disclosures[-MAX_RECENT_DISCLOSURES:]
    await kv.put(RECENT_DISCLOSURES_KEY, json.dumps(trimmed).encode())


def _extract_draft_output(response) -> DraftOutput:
    if not response.candidates:
        reason = getattr(response.prompt_feedback, "block_reason", None) if response.prompt_feedback else None
        raise RuntimeError(f"Gemini returned no candidates (prompt blocked: {reason})")

    finish_reason = response.candidates[0].finish_reason
    if finish_reason != types.FinishReason.STOP:
        raise RuntimeError(f"Gemini did not finish normally: finish_reason={finish_reason}")

    if response.parsed is not None:
        return response.parsed
    # Fall back to manual parsing if the SDK didn't auto-populate .parsed --
    # response_mime_type=json guarantees response.text is valid JSON either way.
    return DraftOutput.model_validate_json(response.text)


def draft_one(client: "genai.Client", item: NewsScored, recent_disclosures: list[str]) -> DraftOutput:
    """One news item -> one validated DraftOutput. Retries once (not in a
    loop) if the model repeats a recent disclosure verbatim -- bounded, not
    infinite, per "systems over heroes" robustness without runaway cost."""
    retry_note = None
    last_result: Optional[DraftOutput] = None

    for _attempt in range(MAX_DUPLICATE_RETRIES + 1):
        response = client.models.generate_content(
            model=MODEL,
            contents=build_user_prompt(item, recent_disclosures, retry_note),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=DraftOutput,
            ),
        )
        result = _extract_draft_output(response)
        last_result = result
        if not is_duplicate_disclosure(result.archetype_disclosure, recent_disclosures):
            return result

        retry_note = (
            f'Your disclosure ("{result.archetype_disclosure}") repeats one already used. '
            "Write a genuinely different phrasing this time."
        )

    # Exhausted retries -- accept the last result rather than loop forever;
    # a slightly-repeated disclosure is a minor authenticity concern, not a
    # safety one, so it's not worth an unbounded retry loop over.
    return last_result


async def run_once() -> None:
    client = genai.Client()  # reads GEMINI_API_KEY from the environment
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    kv = await _get_or_create_kv(js)
    recent_disclosures = await _load_recent_disclosures(kv)

    sub = await js.pull_subscribe(NEWS_SCORED, durable=CONSUMER_DURABLE_NAME, stream="NEWS")
    try:
        msgs = await sub.fetch(FETCH_BATCH_SIZE, timeout=FETCH_TIMEOUT_SECONDS)
    except NatsTimeoutError:
        msgs = []

    drafted = 0
    for msg in msgs:
        item = NewsScored.model_validate_json(msg.data)
        try:
            result = draft_one(client, item, recent_disclosures)
        except Exception as exc:  # noqa: BLE001 -- log and skip, don't crash the whole run over one item
            print(f"[FAILED] {item.title!r}: {exc}")
            await msg.ack()  # considered, not retried indefinitely by the consumer
            continue

        drafted_post = PostDrafted(
            news_id=item.news_id,
            source_link=item.link,
            draft_text=result.draft_text,
            archetype_disclosure=result.archetype_disclosure,
            drafted_at=datetime.now(timezone.utc),
        )
        await js.publish(POST_DRAFTED, drafted_post.model_dump_json().encode())
        recent_disclosures.append(result.archetype_disclosure)
        drafted += 1
        await msg.ack()
        print(f"[drafted] {item.title!r}")
        print(f"  -> {result.draft_text}")

    await _save_recent_disclosures(kv, recent_disclosures)
    print(f"Done. drafted={drafted} considered={len(msgs)}")
    await nc.close()


if __name__ == "__main__":
    asyncio.run(run_once())
