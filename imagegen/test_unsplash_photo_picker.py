from unittest.mock import Mock, patch

import pytest

from imagegen.unsplash_photo_picker import (
    PhotoPickError,
    build_attribution,
    build_search_query,
    get_post_photo,
    search_photo,
    track_download,
)


class TestBuildSearchQuery:
    def test_matches_known_keyword(self):
        assert build_search_query("Riding a jeepney home after a long shift") == "Manila jeepney commute"

    def test_matches_case_insensitively(self):
        assert build_search_query("The BPO industry is booming") == "call center office Philippines"

    def test_falls_back_to_default_query(self):
        assert build_search_query("Something with no matching theme keywords") == "Philippines office worker"


def _mock_response(json_data, raise_for_status=None):
    resp = Mock()
    resp.json.return_value = json_data
    resp.raise_for_status = raise_for_status or Mock()
    return resp


class TestSearchPhoto:
    @patch("imagegen.unsplash_photo_picker.requests.get")
    def test_returns_first_result(self, mock_get):
        photo = {"id": "abc123", "urls": {"regular": "https://img.example/abc.jpg"}}
        mock_get.return_value = _mock_response({"results": [photo]})

        result = search_photo("key", "some query")
        assert result == photo
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Client-ID key"
        assert mock_get.call_args.kwargs["params"]["query"] == "some query"

    @patch("imagegen.unsplash_photo_picker.requests.get")
    def test_no_results_raises_photo_pick_error(self, mock_get):
        mock_get.return_value = _mock_response({"results": []})
        with pytest.raises(PhotoPickError, match="No Unsplash results"):
            search_photo("key", "some query")


class TestTrackDownload:
    @patch("imagegen.unsplash_photo_picker.requests.get")
    def test_pings_download_location_with_auth_header(self, mock_get):
        mock_get.return_value = _mock_response({})
        photo = {"links": {"download_location": "https://api.unsplash.com/photos/abc/download"}}

        track_download("key", photo)

        mock_get.assert_called_once_with(
            "https://api.unsplash.com/photos/abc/download",
            headers={"Authorization": "Client-ID key"},
            timeout=15,
        )


class TestBuildAttribution:
    def test_includes_photographer_name_and_utm_tagged_profile_link(self):
        photo = {"user": {"name": "Jane Doe", "links": {"html": "https://unsplash.com/@janedoe"}}}
        attribution = build_attribution(photo)
        assert "Jane Doe" in attribution
        assert "Unsplash" in attribution
        assert "https://unsplash.com/@janedoe?utm_source=nats-linkedin-automation&utm_medium=referral" in attribution


class TestGetPostPhoto:
    @patch("imagegen.unsplash_photo_picker.requests.get")
    def test_returns_bytes_mime_type_and_attribution(self, mock_get):
        photo = {
            "id": "abc123",
            "urls": {"regular": "https://img.example/abc.jpg"},
            "links": {"download_location": "https://api.unsplash.com/photos/abc/download"},
            "user": {"name": "Jane Doe", "links": {"html": "https://unsplash.com/@janedoe"}},
        }
        search_resp = _mock_response({"results": [photo]})
        download_track_resp = _mock_response({})
        image_resp = Mock(content=b"raw-jpeg-bytes", raise_for_status=Mock())
        mock_get.side_effect = [search_resp, download_track_resp, image_resp]

        image_bytes, mime_type, attribution = get_post_photo("key", "Riding a jeepney home")

        assert image_bytes == b"raw-jpeg-bytes"
        assert mime_type == "image/jpeg"
        assert "Jane Doe" in attribution
