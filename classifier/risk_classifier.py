"""Risk-classifier service: post.drafted -> post.approved OR post.pending_review.

This is the single gate protecting against the cyber-libel exposure
described in the design doc's Constraints -- the classifier decides for
itself which subject to publish to, no separate router (plan-eng-review,
architecture issue 2).

Gate scope (confirmed with the user, narrowing Premise 4's literal OR):
route to review only when the post (a) names a specific INDIVIDUAL with an
attributed claim, (b) names a specific PRIVATE COMPANY with a negative
claim, or (c) asserts any corruption/wrongdoing claim at all. A neutral
mention of a government AGENCY (DOLE, POCB, DMW, ...) in a positive/factual
context is NOT gated -- that's the actual libel rationale in Constraints
(identifiable people/companies), not "any name at all."

Defense in depth: an LLM verdict alone is what the design doc's Open
Questions flagged as the single biggest unresolved reliability risk (a
false negative here is a real legal exposure event, not just a quality
miss). A cheap deterministic keyword backstop runs alongside the LLM call
and can only push toward "risky", never override it toward "safe" -- same
"cheap heuristic first" pattern as the scorer.

Uses Gemini, same as the drafter -- see the design doc's Windows/UTF-8
Constraint; PYTHONUTF8=1 must be set in whatever environment runs this.

Run: .venv/Scripts/python.exe classifier/risk_classifier.py
"""
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import nats
from dotenv import load_dotenv
from google import genai
from google.genai import types
from nats.errors import TimeoutError as NatsTimeoutError
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import POST_APPROVED, POST_DRAFTED, POST_PENDING_REVIEW, ClassifiedPost, PostDrafted  # noqa: E402

load_dotenv()

NATS_URL = "nats://localhost:4222"
CONSUMER_DURABLE_NAME = "classifier-post-drafted"
FETCH_BATCH_SIZE = 10
FETCH_TIMEOUT_SECONDS = 5

MODEL = "gemini-3.5-flash-lite"  # free tier -- see drafter/post_drafter.py for why (20 req/day cap on the non-lite model)

# Deterministic backstop -- unambiguous corruption/wrongdoing vocabulary.
# Word-boundary matching, same technique as the scorer's keyword check.
BACKSTOP_KEYWORDS = [
    "corrupt", "corruption", "bribery", "bribe", "fraud", "fraudulent",
    "embezzle", "embezzlement", "anomalous", "anomaly", "illegal",
    "unlawful", "scam", "kickback", "graft",
]
_COMPILED_BACKSTOP = [
    (kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)) for kw in BACKSTOP_KEYWORDS
]

SYSTEM_PROMPT = """You are a legal-risk classifier for LinkedIn posts about Philippine labor and corporate-worker issues. Decide whether a drafted post is safe to auto-publish or must go to human review first.

Route to review (is_risky = true) if the post does ANY of the following:
1. Names a specific INDIVIDUAL person (a named official, executive, or private individual) and attributes a specific claim, action, or accusation to them.
2. Names a specific PRIVATE COMPANY and attributes a negative action, wrongdoing, or controversy to them.
3. Asserts any factual claim of corruption, fraud, bribery, or other wrongdoing -- whether or not a specific name is attached.

Do NOT route to review merely because the post neutrally mentions a government AGENCY or institution (e.g. DOLE, POCB, DMW, PSA) by name in a positive or purely factual context with no accusation attached. Institutional mentions without wrongdoing claims are safe (is_risky = false).

When genuinely uncertain, err toward is_risky = true -- a false positive costs a few minutes of manual review; a false negative is a potential cyber libel exposure event."""


class ClassifierOutput(BaseModel):
    is_risky: bool
    reason: str


def check_backstop_keywords(text: str) -> tuple[bool, list[str]]:
    matched = [kw for kw, pattern in _COMPILED_BACKSTOP if pattern.search(text)]
    return bool(matched), matched


def build_user_prompt(item: PostDrafted) -> str:
    return f"""Drafted post to classify:

{item.draft_text}

Source: {item.source_link}

Classify this post now."""


def _extract_classifier_output(response) -> ClassifierOutput:
    if not response.candidates:
        reason = getattr(response.prompt_feedback, "block_reason", None) if response.prompt_feedback else None
        raise RuntimeError(f"Gemini returned no candidates (prompt blocked: {reason})")

    finish_reason = response.candidates[0].finish_reason
    if finish_reason != types.FinishReason.STOP:
        raise RuntimeError(f"Gemini did not finish normally: finish_reason={finish_reason}")

    if response.parsed is not None:
        return response.parsed
    return ClassifierOutput.model_validate_json(response.text)


def classify_one(client: "genai.Client", item: PostDrafted) -> ClassifiedPost:
    backstop_triggered, backstop_matches = check_backstop_keywords(item.draft_text)

    response = client.models.generate_content(
        model=MODEL,
        contents=build_user_prompt(item),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ClassifierOutput,
        ),
    )
    llm_result = _extract_classifier_output(response)

    is_risky = backstop_triggered or llm_result.is_risky
    if backstop_triggered:
        reason = f"Backstop keyword match: {', '.join(backstop_matches)}. LLM: {llm_result.reason}"
    else:
        reason = llm_result.reason

    return ClassifiedPost(
        news_id=item.news_id,
        source_link=item.source_link,
        draft_text=item.draft_text,
        archetype_disclosure=item.archetype_disclosure,
        verdict="risky" if is_risky else "safe",
        verdict_reason=reason,
        classified_at=datetime.now(timezone.utc),
        reviewed_by="auto",
    )


async def run_once() -> None:
    client = genai.Client()  # reads GEMINI_API_KEY from the environment
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    sub = await js.pull_subscribe(POST_DRAFTED, durable=CONSUMER_DURABLE_NAME, stream="POSTS")
    try:
        msgs = await sub.fetch(FETCH_BATCH_SIZE, timeout=FETCH_TIMEOUT_SECONDS)
    except NatsTimeoutError:
        msgs = []

    approved = 0
    pending = 0
    for msg in msgs:
        item = PostDrafted.model_validate_json(msg.data)
        try:
            classified = classify_one(client, item)
        except Exception as exc:  # noqa: BLE001 -- log and skip, don't crash the whole run over one item
            print(f"[FAILED] news_id={item.news_id}: {exc}")
            await msg.ack()
            continue

        subject = POST_PENDING_REVIEW if classified.verdict == "risky" else POST_APPROVED
        await js.publish(subject, classified.model_dump_json().encode())
        await msg.ack()

        if classified.verdict == "risky":
            pending += 1
            print(f"[pending_review] news_id={item.news_id}: {classified.verdict_reason}")
        else:
            approved += 1
            print(f"[approved] news_id={item.news_id}: {classified.verdict_reason}")

    print(f"Done. approved={approved} pending_review={pending} considered={len(msgs)}")
    await nc.close()


if __name__ == "__main__":
    asyncio.run(run_once())
