import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from drafter.post_drafter import (  # noqa: E402
    DraftOutput,
    build_user_prompt,
    draft_one,
    is_duplicate_disclosure,
)
from schema.messages import NewsScored, compute_news_id  # noqa: E402


def make_scored_item(title="DOLE tightens wage order compliance", excerpt="New guidelines for workers.") -> NewsScored:
    link = "https://www.philstar.com/nation/2026/09/01/1000001/example"
    return NewsScored(
        news_id=compute_news_id(link),
        feed="nation",
        title=title,
        link=link,
        excerpt=excerpt,
        author=None,
        guid=link,
        published_at=datetime.now(timezone.utc),
        scraped_at=datetime.now(timezone.utc),
        score=6.0,
        score_reason="test fixture",
    )


class TestIsDuplicateDisclosure:
    def test_exact_match_case_insensitive(self):
        recent = ["Meet 'Jana' — a composite, not a real person."]
        candidate = "meet 'jana' — a composite, not a real person."
        assert is_duplicate_disclosure(candidate, recent) is True

    def test_different_text_is_not_duplicate(self):
        recent = ["Meet 'Jana' — a composite, not a real person."]
        candidate = "This is a fictional composite based on real worker stories."
        assert is_duplicate_disclosure(candidate, recent) is False

    def test_empty_recent_list_never_duplicate(self):
        assert is_duplicate_disclosure("Anything at all", []) is False


class TestBuildUserPrompt:
    def test_includes_title_excerpt_and_link(self):
        item = make_scored_item()
        prompt = build_user_prompt(item, [])
        assert item.title in prompt
        assert item.excerpt in prompt
        assert item.link in prompt

    def test_includes_recent_disclosures(self):
        item = make_scored_item()
        prompt = build_user_prompt(item, ["Meet 'Mark' — composite, not real."])
        assert "Meet 'Mark'" in prompt

    def test_shows_none_yet_when_no_recent_disclosures(self):
        item = make_scored_item()
        prompt = build_user_prompt(item, [])
        assert "(none yet)" in prompt

    def test_includes_retry_note_when_given(self):
        item = make_scored_item()
        prompt = build_user_prompt(item, [], retry_note="Try again with different wording.")
        assert "Try again with different wording." in prompt


class TestDraftOne:
    def _mock_response(self, draft_text, disclosure, finish_reason=types.FinishReason.STOP, parsed_populated=True, has_candidates=True):
        response = MagicMock()
        if not has_candidates:
            response.candidates = []
            response.prompt_feedback = MagicMock(block_reason="SAFETY")
            return response

        candidate = MagicMock()
        candidate.finish_reason = finish_reason
        response.candidates = [candidate]

        output = DraftOutput(draft_text=draft_text, archetype_disclosure=disclosure)
        response.parsed = output if parsed_populated else None
        response.text = output.model_dump_json()
        return response

    def test_returns_result_when_not_duplicate(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(
            "Meet 'Jana'... a story about wages.", "Meet 'Jana' — a composite, not a real person."
        )
        item = make_scored_item()
        result = draft_one(client, item, recent_disclosures=[])
        assert result.archetype_disclosure == "Meet 'Jana' — a composite, not a real person."
        assert client.models.generate_content.call_count == 1

    def test_falls_back_to_text_when_parsed_not_populated(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(
            "Fallback draft", "Fallback disclosure line.", parsed_populated=False
        )
        item = make_scored_item()
        result = draft_one(client, item, recent_disclosures=[])
        assert result.draft_text == "Fallback draft"
        assert result.archetype_disclosure == "Fallback disclosure line."

    def test_retries_once_on_duplicate_then_accepts_new_phrasing(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self._mock_response("First draft", "Meet 'Jana' — a composite, not a real person."),
            self._mock_response("Second draft", "This is a fictional worker archetype, not a real account."),
        ]
        item = make_scored_item()
        recent = ["Meet 'Jana' — a composite, not a real person."]
        result = draft_one(client, item, recent_disclosures=recent)
        assert result.draft_text == "Second draft"
        assert client.models.generate_content.call_count == 2

    def test_gives_up_after_max_retries_and_returns_last_result(self):
        client = MagicMock()
        # Every call repeats the same disclosure already in `recent` -- should
        # not loop forever, just accept the last attempt after 1 retry.
        client.models.generate_content.return_value = self._mock_response(
            "Repeated draft", "Meet 'Jana' — a composite, not a real person."
        )
        item = make_scored_item()
        recent = ["Meet 'Jana' — a composite, not a real person."]
        result = draft_one(client, item, recent_disclosures=recent)
        assert result.draft_text == "Repeated draft"
        assert client.models.generate_content.call_count == 2  # 1 initial + 1 retry, then gives up

    def test_raises_on_non_stop_finish_reason(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(
            "unused", "unused", finish_reason=types.FinishReason.SAFETY
        )
        item = make_scored_item()
        with pytest.raises(RuntimeError, match="did not finish normally"):
            draft_one(client, item, recent_disclosures=[])

    def test_raises_when_prompt_blocked_with_no_candidates(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(
            "unused", "unused", has_candidates=False
        )
        item = make_scored_item()
        with pytest.raises(RuntimeError, match="no candidates"):
            draft_one(client, item, recent_disclosures=[])
