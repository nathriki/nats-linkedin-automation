import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from approval_bot.telegram_approval_bot import (  # noqa: E402
    build_approval_keyboard,
    delete_pending,
    format_pending_message,
    load_pending,
    parse_callback_data,
    process_approval_decision,
    store_pending,
)
from schema.messages import POST_APPROVED, ClassifiedPost  # noqa: E402


def make_classified(verdict="risky") -> ClassifiedPost:
    return ClassifiedPost(
        news_id="abc123",
        source_link="https://www.philstar.com/nation/test",
        draft_text="Meet 'Rico' — a composite, not a real person. A story about workers.",
        archetype_disclosure="Meet 'Rico' — a composite, not a real person.",
        verdict=verdict,
        verdict_reason="test fixture",
        classified_at=datetime.now(timezone.utc),
        reviewed_by="auto",
    )


class TestFormatAndKeyboard:
    def test_format_includes_draft_text_source_and_reason(self):
        item = make_classified()
        text = format_pending_message(item)
        assert item.draft_text in text
        assert item.source_link in text
        assert item.verdict_reason in text

    def test_keyboard_has_approve_and_reject_with_news_id(self):
        keyboard = build_approval_keyboard("abc123")
        buttons = keyboard.inline_keyboard[0]
        assert buttons[0].callback_data == "approve:abc123"
        assert buttons[1].callback_data == "reject:abc123"


class TestParseCallbackData:
    def test_splits_action_and_news_id(self):
        action, news_id = parse_callback_data("approve:abc123")
        assert action == "approve"
        assert news_id == "abc123"

    def test_news_id_with_colon_is_preserved(self):
        # news_id is a hex hash so this shouldn't occur in practice, but the
        # split should still behave predictably (only split on the FIRST colon).
        action, news_id = parse_callback_data("reject:abc:123")
        assert action == "reject"
        assert news_id == "abc:123"


class TestPendingStore:
    async def test_store_then_load_round_trips(self, fake_kv):
        item = make_classified()
        await store_pending(fake_kv, item)
        loaded = await load_pending(fake_kv, item.news_id)
        assert loaded == item

    async def test_load_missing_returns_none(self, fake_kv):
        assert await load_pending(fake_kv, "nonexistent") is None

    async def test_delete_removes_entry(self, fake_kv):
        item = make_classified()
        await store_pending(fake_kv, item)
        await delete_pending(fake_kv, item.news_id)
        assert await load_pending(fake_kv, item.news_id) is None

    async def test_delete_missing_does_not_raise(self, fake_kv):
        await delete_pending(fake_kv, "nonexistent")  # should not raise


class TestProcessApprovalDecision:
    async def test_approve_publishes_to_post_approved(self, fake_kv, fake_js):
        item = make_classified()
        await store_pending(fake_kv, item)

        text = await process_approval_decision("approve", item.news_id, fake_kv, fake_js)

        assert "Approved" in text
        assert len(fake_js.published) == 1
        subject, payload = fake_js.published[0]
        assert subject == POST_APPROVED
        published = ClassifiedPost.model_validate_json(payload)
        assert published.reviewed_by == "human"
        assert published.news_id == item.news_id

    async def test_approve_removes_from_pending_store(self, fake_kv, fake_js):
        item = make_classified()
        await store_pending(fake_kv, item)
        await process_approval_decision("approve", item.news_id, fake_kv, fake_js)
        assert await load_pending(fake_kv, item.news_id) is None

    async def test_reject_does_not_publish(self, fake_kv, fake_js):
        item = make_classified()
        await store_pending(fake_kv, item)
        text = await process_approval_decision("reject", item.news_id, fake_kv, fake_js)
        assert "Rejected" in text
        assert fake_js.published == []

    async def test_reject_removes_from_pending_store(self, fake_kv, fake_js):
        item = make_classified()
        await store_pending(fake_kv, item)
        await process_approval_decision("reject", item.news_id, fake_kv, fake_js)
        assert await load_pending(fake_kv, item.news_id) is None

    async def test_unknown_news_id_reports_already_handled(self, fake_kv, fake_js):
        text = await process_approval_decision("approve", "never-stored", fake_kv, fake_js)
        assert "already handled" in text
        assert fake_js.published == []

    async def test_double_approve_is_idempotent_not_double_published(self, fake_kv, fake_js):
        item = make_classified()
        await store_pending(fake_kv, item)
        await process_approval_decision("approve", item.news_id, fake_kv, fake_js)
        second_text = await process_approval_decision("approve", item.news_id, fake_kv, fake_js)
        assert "already handled" in second_text
        assert len(fake_js.published) == 1  # not published twice
