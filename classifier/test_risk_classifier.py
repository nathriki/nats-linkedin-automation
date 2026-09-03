import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from classifier.risk_classifier import (  # noqa: E402
    ClassifierOutput,
    build_user_prompt,
    check_backstop_keywords,
    classify_one,
)
from schema.messages import PostDrafted  # noqa: E402


def make_drafted(draft_text="A neutral post about DOLE and POCB partnering to create jobs.") -> PostDrafted:
    return PostDrafted(
        news_id="abc123",
        source_link="https://www.philstar.com/nation/test",
        draft_text=draft_text,
        archetype_disclosure="Meet 'Rico' — a composite, not a real person.",
        drafted_at=datetime.now(timezone.utc),
    )


class TestCheckBackstopKeywords:
    def test_no_match_on_neutral_text(self):
        triggered, matches = check_backstop_keywords("DOLE and POCB partnered to create jobs abroad.")
        assert triggered is False
        assert matches == []

    def test_matches_explicit_corruption_word(self):
        triggered, matches = check_backstop_keywords("The official was accused of bribery.")
        assert triggered is True
        assert "bribery" in matches

    def test_word_boundary_avoids_false_positive_incorruptible(self):
        triggered, matches = check_backstop_keywords("The agency has an incorruptible reputation.")
        assert triggered is False
        assert matches == []

    def test_multiple_keywords_all_captured(self):
        triggered, matches = check_backstop_keywords("Allegations of fraud and graft surfaced today.")
        assert triggered is True
        assert set(matches) == {"fraud", "graft"}


class TestBuildUserPrompt:
    def test_includes_draft_text_and_source(self):
        item = make_drafted()
        prompt = build_user_prompt(item)
        assert item.draft_text in prompt
        assert item.source_link in prompt


class TestClassifyOne:
    def _mock_response(self, is_risky, reason, finish_reason=types.FinishReason.STOP, has_candidates=True):
        response = MagicMock()
        if not has_candidates:
            response.candidates = []
            response.prompt_feedback = MagicMock(block_reason="SAFETY")
            return response
        candidate = MagicMock()
        candidate.finish_reason = finish_reason
        response.candidates = [candidate]
        output = ClassifierOutput(is_risky=is_risky, reason=reason)
        response.parsed = output
        response.text = output.model_dump_json()
        return response

    def test_llm_safe_and_no_backstop_yields_safe(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(False, "Neutral institutional mention.")
        item = make_drafted("DOLE and POCB partnered to create construction jobs abroad.")
        result = classify_one(client, item)
        assert result.verdict == "safe"

    def test_llm_risky_yields_risky(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(
            True, "Names a specific company with a wrongdoing claim."
        )
        item = make_drafted("XYZ Corp was accused of underpaying its workers, sources say.")
        result = classify_one(client, item)
        assert result.verdict == "risky"
        assert "wrongdoing" in result.verdict_reason

    def test_backstop_overrides_llm_safe_verdict(self):
        client = MagicMock()
        # LLM says safe, but the text plainly contains a corruption keyword --
        # backstop must force risky regardless of what the LLM concluded.
        client.models.generate_content.return_value = self._mock_response(False, "Seems fine to me.")
        item = make_drafted("There were allegations of bribery in the contract award.")
        result = classify_one(client, item)
        assert result.verdict == "risky"
        assert "Backstop keyword match" in result.verdict_reason
        assert "bribery" in result.verdict_reason

    def test_preserves_draft_fields_in_output(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(False, "Fine.")
        item = make_drafted()
        result = classify_one(client, item)
        assert result.news_id == item.news_id
        assert result.draft_text == item.draft_text
        assert result.archetype_disclosure == item.archetype_disclosure
        assert result.reviewed_by == "auto"

    def test_raises_on_non_stop_finish_reason(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(
            False, "unused", finish_reason=types.FinishReason.SAFETY
        )
        item = make_drafted()
        with pytest.raises(RuntimeError, match="did not finish normally"):
            classify_one(client, item)

    def test_raises_when_prompt_blocked_with_no_candidates(self):
        client = MagicMock()
        client.models.generate_content.return_value = self._mock_response(False, "unused", has_candidates=False)
        item = make_drafted()
        with pytest.raises(RuntimeError, match="no candidates"):
            classify_one(client, item)
