"""Poster service: post.approved -> LinkedIn.

The last stage. Uses the LinkedIn Posts API (POST /rest/posts) to publish to
the user's own profile via w_member_social -- verified against LinkedIn's
current API docs rather than assumed, given how much SDK/API drift has
already bitten this build (Gemini's structured-output shape and real free
tier, python-telegram-bot's async lifecycle).

Two gates run before every post, both built in Next Step 9:
  1. Kill-switch (approval_bot/kill_switch.py): if paused, this run does
     nothing and leaves messages unacked for the next run to retry once
     unpaused via /unpause.
  2. Dedup-key gate (poster/dedup_store.py): skips (and acks, since it's
     legitimately handled) anything already posted -- closes the JetStream
     at-least-once double-post risk from Constraints.

Run: .venv/Scripts/python.exe poster/linkedin_poster.py
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import nats
import requests
from dotenv import load_dotenv
from nats.errors import TimeoutError as NatsTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from approval_bot.kill_switch import is_paused  # noqa: E402
from imagegen.unsplash_photo_picker import get_post_photo  # noqa: E402
from poster.dedup_store import already_posted, get_or_create_kv, mark_posted  # noqa: E402
from schema.messages import POST_APPROVED, ClassifiedPost  # noqa: E402

load_dotenv()

NATS_URL = "nats://localhost:4222"
CONSUMER_DURABLE_NAME = "poster-post-approved"
FETCH_BATCH_SIZE = 10
FETCH_TIMEOUT_SECONDS = 5

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".linkedin_token.json"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_URL = "https://api.linkedin.com/rest/images"
LINKEDIN_VERSION = "202608"  # YYYYMM, per LinkedIn's Posts API docs

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2
RETRYABLE_STATUS_CODES = {429, 500, 503}

# LinkedIn's Images API doesn't support synchronous upload (confirmed in its
# docs), and a token with only w_member_social can't reliably GET an
# image's processing status back. A single small illustration processes
# near-instantly in practice, so a short fixed wait stands in for polling.
IMAGE_PROCESSING_WAIT_SECONDS = 3


class LinkedInAuthError(Exception):
    """Token missing or expired -- re-run auth/linkedin_oauth.py."""


class LinkedInPostError(Exception):
    """Post creation failed after retries."""


def load_access_token() -> str:
    if not TOKEN_FILE.exists():
        raise LinkedInAuthError(f"{TOKEN_FILE} not found -- run auth/linkedin_oauth.py first.")

    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    expires_at = datetime.fromisoformat(data["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        raise LinkedInAuthError(f"LinkedIn token expired at {expires_at.isoformat()} -- re-run auth/linkedin_oauth.py.")

    return data["access_token"]


def get_author_urn(access_token: str) -> str:
    resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    resp.raise_for_status()
    sub = resp.json()["sub"]
    return f"urn:li:person:{sub}"


def build_post_payload(author_urn: str, commentary: str, image_urn: Optional[str] = None) -> dict:
    payload = {
        "author": author_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        payload["content"] = {"media": {"id": image_urn}}
    return payload


def initialize_image_upload(access_token: str, author_urn: str) -> tuple[str, str]:
    """Registers an image upload with LinkedIn's Images API. Returns
    (upload_url, image_urn)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    body = {"initializeUploadRequest": {"owner": author_urn}}
    resp = requests.post(f"{IMAGES_URL}?action=initializeUpload", headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    value = resp.json()["value"]
    return value["uploadUrl"], value["image"]


def upload_image_bytes(upload_url: str, access_token: str, image_bytes: bytes, mime_type: str) -> None:
    """PUTs the raw image bytes to the URL from initialize_image_upload.
    Unlike LinkedIn's video upload, image upload requires the OAuth token
    in the Authorization header (per LinkedIn's docs)."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": mime_type}
    resp = requests.put(upload_url, headers=headers, data=image_bytes, timeout=30)
    resp.raise_for_status()


def upload_image(access_token: str, author_urn: str, image_bytes: bytes, mime_type: str) -> str:
    """Registers + uploads one image, returns its urn:li:image:... id."""
    upload_url, image_urn = initialize_image_upload(access_token, author_urn)
    upload_image_bytes(upload_url, access_token, image_bytes, mime_type)
    time.sleep(IMAGE_PROCESSING_WAIT_SECONDS)
    return image_urn


def create_post(access_token: str, author_urn: str, commentary: str, image_urn: Optional[str] = None) -> str:
    """Publishes one post, retrying on LinkedIn's documented transient
    status codes (429/500/503). Returns the post URN from the x-restli-id
    response header. Raises LinkedInPostError on exhaustion or a
    non-retryable error."""
    payload = build_post_payload(author_urn, commentary, image_urn=image_urn)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": LINKEDIN_VERSION,
        "Content-Type": "application/json",
    }

    backoff = INITIAL_BACKOFF_SECONDS
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post(POSTS_URL, headers=headers, json=payload, timeout=15)
        if resp.status_code == 201:
            post_urn = resp.headers.get("x-restli-id")
            if not post_urn:
                raise LinkedInPostError(f"201 response but no x-restli-id header: {resp.headers}")
            return post_urn

        if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
            last_exc = LinkedInPostError(f"{resp.status_code}: {resp.text}")
            time.sleep(backoff)
            backoff *= 2
            continue

        raise LinkedInPostError(f"LinkedIn post failed ({resp.status_code}): {resp.text}")

    raise LinkedInPostError(f"Failed after {MAX_RETRIES} attempts: {last_exc}")


def post_one(item: ClassifiedPost, access_token: str, author_urn: str, unsplash_access_key: Optional[str]) -> str:
    """Picks + attaches a stock photo, then posts. The photo is a
    best-effort enhancement, not a gate -- if it fails for ANY reason (no
    key configured, no search results, network error, LinkedIn upload
    error) the post still goes out as text-only rather than being blocked
    over it. Deliberately a bare except: a real post going out matters far
    more than enumerating every exception type Unsplash or requests can
    raise -- an earlier, narrower except on the prior Gemini-based path let
    an uncaught SDK error crash the whole poster and silently drop an
    approved, ready-to-publish post."""
    image_urn = None
    commentary = item.draft_text
    if unsplash_access_key:
        try:
            image_bytes, mime_type, attribution = get_post_photo(unsplash_access_key, item.draft_text)
            image_urn = upload_image(access_token, author_urn, image_bytes, mime_type)
            commentary = f"{item.draft_text}\n\n{attribution}"
        except Exception as exc:  # noqa: BLE001 -- intentional, see docstring
            print(f"[image skipped] news_id={item.news_id}: {exc}")
    else:
        print(f"[image skipped] news_id={item.news_id}: UNSPLASH_ACCESS_KEY not configured")

    return create_post(access_token, author_urn, commentary, image_urn=image_urn)


async def run_once() -> None:
    access_token = load_access_token()
    author_urn = get_author_urn(access_token)
    unsplash_access_key = os.environ.get("UNSPLASH_ACCESS_KEY")

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    posted_kv = await get_or_create_kv(js)

    if await is_paused(js):
        print("Poster is PAUSED by the kill-switch. Run /unpause in Telegram to resume. Doing nothing this run.")
        await nc.close()
        return

    sub = await js.pull_subscribe(POST_APPROVED, durable=CONSUMER_DURABLE_NAME, stream="POSTS")
    try:
        msgs = await sub.fetch(FETCH_BATCH_SIZE, timeout=FETCH_TIMEOUT_SECONDS)
    except NatsTimeoutError:
        msgs = []

    posted = 0
    skipped_already_posted = 0
    failed = 0
    for msg in msgs:
        item = ClassifiedPost.model_validate_json(msg.data)

        if await already_posted(posted_kv, item.news_id):
            skipped_already_posted += 1
            await msg.ack()  # legitimately handled already -- ack, not a retry case
            continue

        try:
            post_urn = post_one(item, access_token, author_urn, unsplash_access_key)
        except LinkedInPostError as exc:
            failed += 1
            print(f"[FAILED] news_id={item.news_id}: {exc}")
            continue  # not acked -- retried on a future run

        await mark_posted(posted_kv, item.news_id)
        await msg.ack()
        posted += 1
        print(f"[posted] news_id={item.news_id} -> {post_urn}")

    print(f"Done. posted={posted} skipped_already_posted={skipped_already_posted} failed={failed} considered={len(msgs)}")
    await nc.close()


if __name__ == "__main__":
    asyncio.run(run_once())
