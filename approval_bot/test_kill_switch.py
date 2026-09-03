import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from approval_bot.kill_switch import (  # noqa: E402
    FLAG_THRESHOLD,
    count_recent_flags,
    is_paused,
    record_flag,
    set_paused,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestCountRecentFlags:
    def test_empty_list_counts_zero(self):
        assert count_recent_flags([], NOW) == 0

    def test_counts_flags_within_window(self):
        flags = [
            {"news_id": "a", "reason": "x", "at": (NOW - timedelta(hours=1)).isoformat()},
            {"news_id": "b", "reason": "y", "at": (NOW - timedelta(hours=23)).isoformat()},
        ]
        assert count_recent_flags(flags, NOW) == 2

    def test_excludes_flags_outside_window(self):
        flags = [
            {"news_id": "a", "reason": "x", "at": (NOW - timedelta(hours=25)).isoformat()},
        ]
        assert count_recent_flags(flags, NOW) == 0

    def test_mixed_in_and_out_of_window(self):
        flags = [
            {"news_id": "a", "reason": "x", "at": (NOW - timedelta(hours=1)).isoformat()},
            {"news_id": "b", "reason": "y", "at": (NOW - timedelta(hours=48)).isoformat()},
        ]
        assert count_recent_flags(flags, NOW) == 1


class TestRecordFlag:
    @pytest.mark.asyncio
    async def test_single_flag_does_not_trigger(self, fake_js):
        triggered = await record_flag(fake_js, "news-1", "seems off", at=NOW)
        assert triggered is False
        assert await is_paused(fake_js) is False

    @pytest.mark.asyncio
    async def test_second_flag_within_window_triggers(self, fake_js):
        await record_flag(fake_js, "news-1", "first", at=NOW)
        triggered = await record_flag(fake_js, "news-2", "second", at=NOW + timedelta(hours=1))
        assert triggered is True
        assert await is_paused(fake_js) is True

    @pytest.mark.asyncio
    async def test_two_flags_outside_each_others_window_does_not_trigger(self, fake_js):
        await record_flag(fake_js, "news-1", "first", at=NOW)
        triggered = await record_flag(fake_js, "news-2", "second", at=NOW + timedelta(hours=30))
        assert triggered is False
        assert await is_paused(fake_js) is False

    @pytest.mark.asyncio
    async def test_threshold_constant_is_two(self):
        # Documents the design doc's chosen threshold explicitly, so a change
        # to this constant is a visible, deliberate test failure, not silent.
        assert FLAG_THRESHOLD == 2


class TestPauseState:
    @pytest.mark.asyncio
    async def test_defaults_to_not_paused(self, fake_js):
        assert await is_paused(fake_js) is False

    @pytest.mark.asyncio
    async def test_set_paused_true_then_false(self, fake_js):
        await set_paused(fake_js, True)
        assert await is_paused(fake_js) is True
        await set_paused(fake_js, False)
        assert await is_paused(fake_js) is False
