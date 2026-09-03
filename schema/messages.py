"""Frozen NATS subject/payload schema for the pipeline.

Every service (scraper, scorer, drafter, risk-classifier, approval bot,
poster) imports from here rather than redefining message shapes -- this is
the single source of truth the design doc's "subject schema" step produces.

Subjects and their producer -> consumer flow:

    news.raw           scraper       -> scorer
    news.raw.failed    scraper       -> (ops/heartbeat, not yet built)
    news.scored        scorer        -> drafter
    post.drafted       drafter       -> risk-classifier
    post.pending_review risk-classifier -> approval bot
    post.approved       risk-classifier OR approval bot -> poster
"""
import hashlib
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

# --- Subjects -----------------------------------------------------------

NEWS_RAW = "news.raw"
NEWS_RAW_FAILED = "news.raw.failed"
NEWS_SCORED = "news.scored"
POST_DRAFTED = "post.drafted"
POST_PENDING_REVIEW = "post.pending_review"
POST_APPROVED = "post.approved"

ALL_SUBJECTS = (
    NEWS_RAW,
    NEWS_RAW_FAILED,
    NEWS_SCORED,
    POST_DRAFTED,
    POST_PENDING_REVIEW,
    POST_APPROVED,
)


def compute_news_id(link: str) -> str:
    """Dedup key for a news item: a hash of the article URL alone.

    Deliberately link-only (not link+pubDate, despite the design doc's
    original suggestion): the URL is the stable identifier for an article,
    across every source this scraper reads from. Hashing in the publish
    timestamp too would let a source-side timestamp correction (rare, but
    seen in the wild) produce a new id for the same article -- exactly the
    double-post risk this key exists to prevent. Link-only is the more
    conservative choice.
    """
    normalized = link.strip().rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# --- Payloads -------------------------------------------------------------


class NewsRaw(BaseModel):
    """Published by the scraper for each RSS item, from whichever source
    produced it.

    Deliberately excludes full article text -- the scraper only ever reads
    the RSS feed itself (title + short <description> excerpt + link), never
    the linked article page. See Constraints in the design doc.

    Inquirer is a source with no excerpt: their terms restrict external
    reuse to headlines only (no description text) without separate
    syndication clearance, so the scraper always publishes excerpt="" for
    that source -- narrower than the other two, not a bug.
    """

    news_id: str  # compute_news_id(link)
    source: Literal["philstar", "gma", "inquirer"] = "philstar"
    feed: Literal["nation", "business"]
    title: str
    link: str
    excerpt: str  # RSS <description>, already a short excerpt
    author: Optional[str] = None
    guid: str
    published_at: datetime  # parsed RSS <pubDate>
    scraped_at: datetime


class NewsRawFailed(BaseModel):
    """Published by the scraper when a source fetch fails persistently
    (after retry/backoff) -- distinct from a genuine zero-news day."""

    source: Literal["philstar", "gma", "inquirer"] = "philstar"
    feed: Literal["nation", "business"]
    error: str
    occurred_at: datetime
    retry_count: int


class NewsScored(NewsRaw):
    """NewsRaw plus a relevance score. The scorer is a cheap heuristic (no
    LLM call) -- see the design doc's performance constraint."""

    score: float
    score_reason: str


class PostDrafted(BaseModel):
    """Published by the drafter after generating a storytelling post."""

    news_id: str
    source_link: str
    draft_text: str
    archetype_disclosure: str  # the actual disclosure line used in this post
    drafted_at: datetime


class ClassifiedPost(BaseModel):
    """Published to EITHER post.pending_review or post.approved -- the
    risk-classifier picks the subject based on its own verdict (no separate
    router service, per the eng review's architecture decision)."""

    news_id: str
    source_link: str
    draft_text: str
    archetype_disclosure: str
    verdict: Literal["safe", "risky"]
    verdict_reason: str
    classified_at: datetime
    # Set when a human approves a pending_review post; "auto" when the
    # classifier itself routed straight to post.approved. Needed for the
    # audit trail the design doc's JetStream persistence is meant to provide.
    reviewed_by: Literal["auto", "human"] = "auto"
