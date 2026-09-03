"""Create (or update) the JetStream streams the pipeline depends on.

Idempotent -- safe to re-run any time the server restarts or a stream's
config changes. Run with the NATS server already up:

    .venv/Scripts/python.exe nats/setup_streams.py
"""
import asyncio
import sys

import nats
from nats.js.api import RetentionPolicy, StreamConfig

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from schema.messages import NEWS_RAW, NEWS_RAW_FAILED, NEWS_SCORED, POST_APPROVED, POST_DRAFTED, POST_PENDING_REVIEW  # noqa: E402

NATS_URL = "nats://localhost:4222"

# Limits retention (not WorkQueue) so messages persist for the audit trail
# the design doc calls for, independent of whether/when a consumer acks them.
NINETY_DAYS_SECONDS = 90 * 24 * 60 * 60

STREAMS = [
    StreamConfig(
        name="NEWS",
        subjects=[NEWS_RAW, NEWS_RAW_FAILED, NEWS_SCORED],
        retention=RetentionPolicy.LIMITS,
        max_age=NINETY_DAYS_SECONDS,
    ),
    StreamConfig(
        name="POSTS",
        subjects=[POST_DRAFTED, POST_PENDING_REVIEW, POST_APPROVED],
        retention=RetentionPolicy.LIMITS,
        max_age=NINETY_DAYS_SECONDS,
    ),
]


async def main():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    for config in STREAMS:
        try:
            await js.add_stream(config)
            print(f"Created stream {config.name} (subjects: {config.subjects})")
        except Exception as exc:
            if "already in use" in str(exc) or "already exists" in str(exc):
                await js.update_stream(config)
                print(f"Updated existing stream {config.name} (subjects: {config.subjects})")
            else:
                raise

    await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
