import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poster.dedup_store import already_posted, mark_posted  # noqa: E402


class TestDedupStore:
    async def test_unposted_news_id_is_not_already_posted(self, fake_kv):
        assert await already_posted(fake_kv, "news-1") is False

    async def test_marked_news_id_is_already_posted(self, fake_kv):
        await mark_posted(fake_kv, "news-1")
        assert await already_posted(fake_kv, "news-1") is True

    async def test_marking_one_id_does_not_affect_another(self, fake_kv):
        await mark_posted(fake_kv, "news-1")
        assert await already_posted(fake_kv, "news-2") is False
