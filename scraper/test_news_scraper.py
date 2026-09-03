import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema.messages import compute_news_id  # noqa: E402
from scraper.news_scraper import FEEDS, FeedFetchError, fetch_feed_with_retry, parse_feed_xml  # noqa: E402

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel>
<title>philstar.com - RSS Nation</title>
<item>
<title>DOLE tightens wage order compliance</title>
<link>https://www.philstar.com/nation/2026/09/01/1000001/dole-tightens-wage-order-compliance</link>
<author>Jane Reporter</author>
<description>The labor department rolled out new guidelines this week.</description>
<pubDate>Tue, 1 Sep 2026 08:52:00 +0800</pubDate>
<guid isPermaLink="true">https://www.philstar.com/nation/2026/09/01/1000001/dole-tightens-wage-order-compliance</guid>
</item>
<item>
<title>BPO group petitions for wage hike</title>
<link>https://www.philstar.com/nation/2026/09/01/1000002/bpo-group-petitions-wage-hike</link>
<description>A workers network is asking for a daily minimum wage increase.</description>
<pubDate>Tue, 1 Sep 2026 07:00:00 +0800</pubDate>
<guid isPermaLink="true">https://www.philstar.com/nation/2026/09/01/1000002/bpo-group-petitions-wage-hike</guid>
</item>
<item>
<title>Malformed item with no link</title>
<description>This item is missing a link and should be skipped.</description>
</item>
</channel></rss>"""

SAMPLE_FEED_WITH_HTML_EXCERPT = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel>
<item>
<title>DepEd to propose new rules</title>
<link>https://www.gmanetwork.com/news/topstories/nation/1001056/deped-story</link>
<description><![CDATA[<img width="auto" height="150" src="https://images.gmanews.tv/pic.jpg"/><br/>Education Secretary announced new guidelines.]]></description>
<pubDate>Tue, 1 Sep 2026 08:52:00 +0800</pubDate>
<guid isPermaLink="false">tag:gmanetwork.com:news_story-1001056</guid>
</item>
</channel></rss>"""


class TestFeedsConfig:
    def test_no_source_offers_excerpt_except_inquirer_being_excluded(self):
        # Inquirer's terms restrict reuse to headlines only -- every
        # Inquirer feed entry must have include_excerpt=False, and every
        # other source's entries must have it True.
        for source, _feed_name, _url, include_excerpt in FEEDS:
            if source == "inquirer":
                assert include_excerpt is False
            else:
                assert include_excerpt is True

    def test_rappler_is_not_a_source(self):
        # Rappler's robots.txt names anthropic-ai/ClaudeBot as disallowed
        # -- deliberately excluded, not an oversight.
        sources = {source for source, _feed_name, _url, _include_excerpt in FEEDS}
        assert "rappler" not in sources


class TestParseFeedXml:
    def test_extracts_all_fields(self):
        items = parse_feed_xml(SAMPLE_FEED, source="philstar", feed_name="nation", include_excerpt=True)
        assert len(items) == 2  # the malformed 3rd item is skipped

        first = items[0]
        assert first.title == "DOLE tightens wage order compliance"
        assert first.link == "https://www.philstar.com/nation/2026/09/01/1000001/dole-tightens-wage-order-compliance"
        assert first.source == "philstar"
        assert first.author == "Jane Reporter"
        assert first.excerpt == "The labor department rolled out new guidelines this week."
        assert first.feed == "nation"
        assert first.news_id == compute_news_id(first.link)
        assert first.published_at.year == 2026
        assert first.published_at.month == 9
        assert first.published_at.day == 1

    def test_missing_author_is_none(self):
        items = parse_feed_xml(SAMPLE_FEED, source="philstar", feed_name="nation", include_excerpt=True)
        second = items[1]
        assert second.author is None

    def test_skips_item_missing_title_or_link(self):
        items = parse_feed_xml(SAMPLE_FEED, source="philstar", feed_name="nation", include_excerpt=True)
        titles = [i.title for i in items]
        assert "Malformed item with no link" not in titles

    def test_missing_pubdate_falls_back_to_scraped_at(self):
        xml_no_date = """<?xml version="1.0"?><rss><channel>
        <item><title>No date item</title><link>https://www.philstar.com/x/1</link></item>
        </channel></rss>"""
        fixed_time = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        items = parse_feed_xml(
            xml_no_date, source="philstar", feed_name="nation", include_excerpt=True, scraped_at=fixed_time
        )
        assert items[0].published_at == fixed_time

    def test_two_different_links_get_different_dedup_ids(self):
        items = parse_feed_xml(SAMPLE_FEED, source="philstar", feed_name="nation", include_excerpt=True)
        assert items[0].news_id != items[1].news_id

    def test_same_link_is_deterministic(self):
        items_a = parse_feed_xml(SAMPLE_FEED, source="philstar", feed_name="nation", include_excerpt=True)
        items_b = parse_feed_xml(SAMPLE_FEED, source="philstar", feed_name="nation", include_excerpt=True)
        assert items_a[0].news_id == items_b[0].news_id

    def test_include_excerpt_false_always_publishes_empty_excerpt(self):
        # Inquirer's headline-only restriction: even though the feed HAS a
        # <description>, it must never end up in the published item.
        items = parse_feed_xml(SAMPLE_FEED, source="inquirer", feed_name="nation", include_excerpt=False)
        assert all(item.excerpt == "" for item in items)

    def test_source_is_set_on_every_item(self):
        items = parse_feed_xml(SAMPLE_FEED, source="gma", feed_name="business", include_excerpt=True)
        assert all(item.source == "gma" for item in items)

    def test_html_markup_stripped_from_excerpt(self):
        items = parse_feed_xml(
            SAMPLE_FEED_WITH_HTML_EXCERPT, source="gma", feed_name="nation", include_excerpt=True
        )
        assert items[0].excerpt == "Education Secretary announced new guidelines."

    def test_leading_whitespace_before_xml_declaration_does_not_crash(self):
        # Inquirer's business feed sends a stray space+tab before <?xml,
        # which a strict XML parser otherwise rejects outright.
        prefixed = " \t" + SAMPLE_FEED
        items = parse_feed_xml(prefixed, source="inquirer", feed_name="business", include_excerpt=False)
        assert len(items) == 2


class TestFetchFeedWithRetry:
    def _mock_response(self, status_ok=True, text="<rss></rss>"):
        resp = Mock()
        if status_ok:
            resp.raise_for_status = Mock()
            resp.text = text
        else:
            resp.raise_for_status = Mock(side_effect=requests.HTTPError("500 error"))
        return resp

    @patch("scraper.news_scraper.time.sleep")
    @patch("scraper.news_scraper.requests.get")
    def test_succeeds_on_first_try(self, mock_get, mock_sleep):
        mock_get.return_value = self._mock_response(text="<rss>ok</rss>")
        result = fetch_feed_with_retry("https://example.test/feed")
        assert result == "<rss>ok</rss>"
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("scraper.news_scraper.time.sleep")
    @patch("scraper.news_scraper.requests.get")
    def test_retries_then_succeeds(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.ConnectionError("network blip"),
            self._mock_response(text="<rss>recovered</rss>"),
        ]
        result = fetch_feed_with_retry("https://example.test/feed")
        assert result == "<rss>recovered</rss>"
        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("scraper.news_scraper.time.sleep")
    @patch("scraper.news_scraper.requests.get")
    def test_exhausts_retries_and_raises(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.ConnectionError("persistent outage")
        with pytest.raises(FeedFetchError):
            fetch_feed_with_retry("https://example.test/feed")
        assert mock_get.call_count == 3  # MAX_RETRIES

    @patch("scraper.news_scraper.time.sleep")
    @patch("scraper.news_scraper.requests.get")
    def test_http_error_status_triggers_retry_path(self, mock_get, mock_sleep):
        mock_get.return_value = self._mock_response(status_ok=False)
        with pytest.raises(FeedFetchError):
            fetch_feed_with_retry("https://example.test/feed")
        assert mock_get.call_count == 3
