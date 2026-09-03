"""One-off smoke test: publish a NewsRaw message, read it back via a
JetStream pull consumer, and confirm the round trip. Not a pytest suite --
that comes with the actual scraper/scorer services. This just proves the
subject/schema contract end-to-end before building against it.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import nats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import NEWS_RAW, NewsRaw, compute_news_id  # noqa: E402

NATS_URL = "nats://localhost:4222"


async def main():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    link = "https://www.philstar.com/nation/2026/09/01/smoke-test-story"
    message = NewsRaw(
        news_id=compute_news_id(link),
        feed="nation",
        title="Smoke test story",
        link=link,
        excerpt="This is a smoke-test excerpt, not a real article.",
        author=None,
        guid=link,
        published_at=datetime.now(timezone.utc),
        scraped_at=datetime.now(timezone.utc),
    )

    ack = await js.publish(NEWS_RAW, message.model_dump_json().encode())
    print(f"Published to {NEWS_RAW}: stream={ack.stream}, seq={ack.seq}")

    sub = await js.pull_subscribe(NEWS_RAW, durable="smoke-test-consumer", stream="NEWS")
    msgs = await sub.fetch(1, timeout=5)
    for msg in msgs:
        received = NewsRaw.model_validate_json(msg.data)
        assert received.news_id == message.news_id
        assert received.link == link
        print(f"Round-trip OK: news_id={received.news_id}, title={received.title!r}")
        await msg.ack()

    # Clean up the throwaway consumer so re-running this script doesn't
    # accumulate durable consumers.
    await js.delete_consumer("NEWS", "smoke-test-consumer")

    await nc.close()
    print("SMOKE_TEST_OK")


if __name__ == "__main__":
    asyncio.run(main())
