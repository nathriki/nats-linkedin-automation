import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import NewsRaw, compute_news_id  # noqa: E402
from scorer.heuristic_scorer import RECENCY_FLOOR, score_candidate  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_item(title: str, excerpt: str = "", hours_old: float = 0.0) -> NewsRaw:
    link = f"https://www.philstar.com/nation/test/{abs(hash((title, excerpt)))}"
    published_at = NOW - timedelta(hours=hours_old)
    return NewsRaw(
        news_id=compute_news_id(link),
        feed="nation",
        title=title,
        link=link,
        excerpt=excerpt,
        author=None,
        guid=link,
        published_at=published_at,
        scraped_at=NOW,
    )


class TestScoreCandidate:
    def test_off_topic_item_scores_zero(self):
        item = make_item(
            "'Pilandok' keeps strength as habagat brings heavy rain",
            "Tropical Depression Pilandok maintained its strength Tuesday.",
        )
        score, reason = score_candidate(item, now=NOW)
        assert score == 0.0
        assert "no relevant keywords" in reason

    def test_on_topic_item_scores_positive(self):
        item = make_item(
            "DOLE tightens wage order compliance nationwide",
            "New guidelines ensure workers get mandated wage increases.",
        )
        score, reason = score_candidate(item, now=NOW)
        assert score > 0.0
        assert "dole" in reason.lower()
        assert "wage" in reason.lower()

    def test_word_boundary_avoids_false_positive_alberto(self):
        item = make_item("Alberto wins mayoral race in provincial town", "A local election story.")
        score, _reason = score_candidate(item, now=NOW)
        assert score == 0.0  # must NOT match "rto" inside "Alberto"

    def test_word_boundary_avoids_false_positive_condole(self):
        item = make_item("Senator moves to condole with flood victims", "A sympathy message.")
        score, _reason = score_candidate(item, now=NOW)
        assert score == 0.0  # must NOT match "dole" inside "condole"

    def test_multiple_keywords_sum(self):
        single = make_item("Workers demand a raise", "")
        multi = make_item("BPO workers demand a wage hike from DOLE", "")
        score_single, _ = score_candidate(single, now=NOW)
        score_multi, _ = score_candidate(multi, now=NOW)
        assert score_multi > score_single

    def test_recency_decay_prefers_newer(self):
        fresh = make_item("DOLE announces new wage order", hours_old=0)
        stale = make_item("DOLE announces new wage order", hours_old=40)
        score_fresh, _ = score_candidate(fresh, now=NOW)
        score_stale, _ = score_candidate(stale, now=NOW)
        assert score_fresh > score_stale

    def test_recency_floor_does_not_reach_zero(self):
        # "DOLE clarifies new memo" matches only "dole" -- one keyword, so
        # the expected score is unambiguous: keyword_score(3.0) * floor.
        ancient = make_item("DOLE clarifies new memo", hours_old=1000)
        score, _ = score_candidate(ancient, now=NOW)
        assert score > 0.0
        assert score == 3.0 * RECENCY_FLOOR

    def test_zero_relevance_ignores_recency_entirely(self):
        fresh_but_offtopic = make_item("Local barangay fiesta draws crowds", hours_old=0)
        score, _ = score_candidate(fresh_but_offtopic, now=NOW)
        assert score == 0.0
