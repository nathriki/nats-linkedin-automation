"""Picks one real stock photo per post via the Unsplash API.

Replaces the earlier Gemini-image-generation path: Gemini has no free tier
for image models (confirmed against Google's own pricing docs), and
Hugging Face's free tier turned out to be only $0.10/month in credits --
nowhere near enough for daily posts. Unsplash's free "Demo" app tier (50
requests/hour, no card required) actually covers this cadence at zero cost.

Trade-off accepted by the user: photos are real stock photography matched
to a coarse keyword guess from the post text, not a story-specific
illustration -- mood-setting, not literal.

Runs at poster time (called from poster/linkedin_poster.py), same as the
Gemini path did, so a post a human rejects at pending_review never spends
an Unsplash request.

Unsplash's API Guidelines require attribution to both the photographer and
Unsplash with a UTM-tagged link back to the photographer's profile, and a
"trigger a download" ping every time a photo is actually used (not just
previewed) -- both handled here.

Run standalone for a quick manual check:
  .venv/Scripts/python.exe imagegen/unsplash_photo_picker.py "some draft text"
"""
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

APP_NAME = "nats-linkedin-automation"
SEARCH_URL = "https://api.unsplash.com/search/photos"

DEFAULT_QUERY = "Philippines office worker"

# Cheap heuristic, consistent with the rest of this pipeline (see the
# scorer): a small keyword -> better-targeted-query lookup, checked in
# order, falling back to a generic query rather than anything fancier.
THEME_KEYWORDS = [
    ("jeepney", "Manila jeepney commute"),
    ("bpo", "call center office Philippines"),
    ("call center", "call center office Philippines"),
    ("commute", "Manila traffic commute"),
    ("traffic", "Manila traffic commute"),
    ("wage", "Philippines office worker payday"),
    ("salary", "Philippines office worker payday"),
    ("pay", "Philippines office worker payday"),
    ("remote work", "work from home laptop"),
    ("work from home", "work from home laptop"),
    ("contractual", "Philippines factory workers"),
    ("layoff", "empty office desk"),
    ("layoffs", "empty office desk"),
]


class PhotoPickError(Exception):
    """No usable photo came back from Unsplash (no results, bad key, network error, etc.)."""


def build_search_query(draft_text: str) -> str:
    lowered = draft_text.lower()
    for keyword, query in THEME_KEYWORDS:
        if keyword in lowered:
            return query
    return DEFAULT_QUERY


def search_photo(access_key: str, query: str) -> dict:
    resp = requests.get(
        SEARCH_URL,
        headers={"Authorization": f"Client-ID {access_key}"},
        params={"query": query, "per_page": 1, "orientation": "landscape"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise PhotoPickError(f"No Unsplash results for query {query!r}")
    return results[0]


def track_download(access_key: str, photo: dict) -> None:
    """Required by Unsplash's API Guidelines every time a photo is
    actually used, distinct from the automatic hotlink view."""
    resp = requests.get(
        photo["links"]["download_location"],
        headers={"Authorization": f"Client-ID {access_key}"},
        timeout=15,
    )
    resp.raise_for_status()


def build_attribution(photo: dict) -> str:
    name = photo["user"]["name"]
    profile_url = f"{photo['user']['links']['html']}?utm_source={APP_NAME}&utm_medium=referral"
    return f"Photo by {name} on Unsplash ({profile_url})"


def get_post_photo(access_key: str, draft_text: str) -> tuple[bytes, str, str]:
    """Returns (image_bytes, mime_type, attribution_line). Raises
    PhotoPickError/requests.RequestException if anything goes wrong --
    callers should treat this as non-fatal and post without a photo
    rather than blocking the whole post over it."""
    query = build_search_query(draft_text)
    photo = search_photo(access_key, query)
    track_download(access_key, photo)

    image_resp = requests.get(photo["urls"]["regular"], timeout=30)
    image_resp.raise_for_status()

    return image_resp.content, "image/jpeg", build_attribution(photo)


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else (
        "A BPO worker rides a jeepney home after a long shift, thinking about rising fares."
    )
    access_key = os.environ["UNSPLASH_ACCESS_KEY"]
    image_bytes, mime_type, attribution = get_post_photo(access_key, text)
    out_path = Path(__file__).resolve().parent / "sample_output.jpg"
    out_path.write_bytes(image_bytes)
    print(f"Saved {len(image_bytes)} bytes ({mime_type}) to {out_path}")
    print(attribution)
