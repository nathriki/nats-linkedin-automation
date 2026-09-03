import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from imagegen.unsplash_photo_picker import PhotoPickError  # noqa: E402
from poster.linkedin_poster import (  # noqa: E402
    LinkedInAuthError,
    LinkedInPostError,
    build_post_payload,
    create_post,
    get_author_urn,
    initialize_image_upload,
    load_access_token,
    post_one,
    upload_image,
    upload_image_bytes,
)
from schema.messages import ClassifiedPost  # noqa: E402


class TestBuildPostPayload:
    def test_has_required_fields(self):
        payload = build_post_payload("urn:li:person:abc123", "Hello world")
        assert payload["author"] == "urn:li:person:abc123"
        assert payload["commentary"] == "Hello world"
        assert payload["visibility"] == "PUBLIC"
        assert payload["lifecycleState"] == "PUBLISHED"
        assert payload["distribution"]["feedDistribution"] == "MAIN_FEED"
        assert payload["isReshareDisabledByAuthor"] is False

    def test_no_image_urn_omits_content_field(self):
        payload = build_post_payload("urn:li:person:abc123", "Hello world")
        assert "content" not in payload

    def test_image_urn_adds_content_media_field(self):
        payload = build_post_payload("urn:li:person:abc123", "Hello world", image_urn="urn:li:image:xyz")
        assert payload["content"] == {"media": {"id": "urn:li:image:xyz"}}


class TestLoadAccessToken:
    def _write_token_file(self, tmp_path, expires_at: datetime, access_token="tok-123"):
        token_file = tmp_path / ".linkedin_token.json"
        token_file.write_text(
            json.dumps({"access_token": access_token, "expires_at": expires_at.isoformat()}),
            encoding="utf-8",
        )
        return token_file

    def test_missing_file_raises_auth_error(self, tmp_path):
        with patch("poster.linkedin_poster.TOKEN_FILE", tmp_path / "nonexistent.json"):
            with pytest.raises(LinkedInAuthError, match="not found"):
                load_access_token()

    def test_expired_token_raises_auth_error(self, tmp_path):
        expired = datetime.now(timezone.utc) - timedelta(days=1)
        token_file = self._write_token_file(tmp_path, expired)
        with patch("poster.linkedin_poster.TOKEN_FILE", token_file):
            with pytest.raises(LinkedInAuthError, match="expired"):
                load_access_token()

    def test_valid_token_returns_access_token(self, tmp_path):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        token_file = self._write_token_file(tmp_path, future, access_token="valid-token")
        with patch("poster.linkedin_poster.TOKEN_FILE", token_file):
            assert load_access_token() == "valid-token"


class TestGetAuthorUrn:
    @patch("poster.linkedin_poster.requests.get")
    def test_builds_person_urn_from_sub(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {"sub": "782bbtaQ"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        urn = get_author_urn("some-token")
        assert urn == "urn:li:person:782bbtaQ"


class TestCreatePost:
    def _mock_response(self, status_code, post_urn=None, text=""):
        resp = Mock()
        resp.status_code = status_code
        resp.text = text
        resp.headers = {"x-restli-id": post_urn} if post_urn else {}
        return resp

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_success_returns_post_urn(self, mock_post, mock_sleep):
        mock_post.return_value = self._mock_response(201, post_urn="urn:li:share:12345")
        urn = create_post("token", "urn:li:person:abc", "Hello")
        assert urn == "urn:li:share:12345"
        assert mock_post.call_count == 1

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_retries_on_429_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            self._mock_response(429, text="rate limited"),
            self._mock_response(201, post_urn="urn:li:share:67890"),
        ]
        urn = create_post("token", "urn:li:person:abc", "Hello")
        assert urn == "urn:li:share:67890"
        assert mock_post.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_retries_on_503_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            self._mock_response(503, text="service unavailable"),
            self._mock_response(201, post_urn="urn:li:share:11111"),
        ]
        urn = create_post("token", "urn:li:person:abc", "Hello")
        assert urn == "urn:li:share:11111"

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_exhausts_retries_and_raises(self, mock_post, mock_sleep):
        mock_post.return_value = self._mock_response(500, text="server error")
        with pytest.raises(LinkedInPostError, match="500"):
            create_post("token", "urn:li:person:abc", "Hello")
        assert mock_post.call_count == 3  # MAX_RETRIES

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_non_retryable_error_fails_immediately(self, mock_post, mock_sleep):
        # 401 EMPTY_ACCESS_TOKEN is not in RETRYABLE_STATUS_CODES -- should
        # not retry 3 times over an auth problem that a retry can't fix.
        mock_post.return_value = self._mock_response(401, text="unauthorized")
        with pytest.raises(LinkedInPostError, match="401"):
            create_post("token", "urn:li:person:abc", "Hello")
        assert mock_post.call_count == 1
        mock_sleep.assert_not_called()

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_missing_x_restli_id_header_raises(self, mock_post, mock_sleep):
        mock_post.return_value = self._mock_response(201, post_urn=None)
        with pytest.raises(LinkedInPostError, match="x-restli-id"):
            create_post("token", "urn:li:person:abc", "Hello")

    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.requests.post")
    def test_passes_image_urn_into_payload(self, mock_post, mock_sleep):
        mock_post.return_value = self._mock_response(201, post_urn="urn:li:share:99")
        create_post("token", "urn:li:person:abc", "Hello", image_urn="urn:li:image:xyz")
        sent_payload = mock_post.call_args.kwargs["json"]
        assert sent_payload["content"] == {"media": {"id": "urn:li:image:xyz"}}


class TestInitializeImageUpload:
    @patch("poster.linkedin_poster.requests.post")
    def test_returns_upload_url_and_image_urn(self, mock_post):
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = {
            "value": {"uploadUrl": "https://upload.example/abc", "image": "urn:li:image:abc"}
        }
        mock_post.return_value = resp

        upload_url, image_urn = initialize_image_upload("token", "urn:li:person:abc")

        assert upload_url == "https://upload.example/abc"
        assert image_urn == "urn:li:image:abc"
        sent_body = mock_post.call_args.kwargs["json"]
        assert sent_body == {"initializeUploadRequest": {"owner": "urn:li:person:abc"}}


class TestUploadImageBytes:
    @patch("poster.linkedin_poster.requests.put")
    def test_puts_bytes_with_auth_header(self, mock_put):
        resp = Mock()
        resp.raise_for_status = Mock()
        mock_put.return_value = resp

        upload_image_bytes("https://upload.example/abc", "token", b"raw-bytes", "image/png")

        mock_put.assert_called_once()
        assert mock_put.call_args.args[0] == "https://upload.example/abc"
        assert mock_put.call_args.kwargs["headers"]["Authorization"] == "Bearer token"
        assert mock_put.call_args.kwargs["data"] == b"raw-bytes"


class TestUploadImage:
    @patch("poster.linkedin_poster.time.sleep")
    @patch("poster.linkedin_poster.upload_image_bytes")
    @patch("poster.linkedin_poster.initialize_image_upload")
    def test_registers_then_uploads_then_returns_urn(self, mock_init, mock_upload_bytes, mock_sleep):
        mock_init.return_value = ("https://upload.example/abc", "urn:li:image:abc")

        image_urn = upload_image("token", "urn:li:person:abc", b"raw-bytes", "image/png")

        assert image_urn == "urn:li:image:abc"
        mock_upload_bytes.assert_called_once_with("https://upload.example/abc", "token", b"raw-bytes", "image/png")


class TestPostOne:
    def _item(self):
        return ClassifiedPost(
            news_id="abc123",
            source_link="https://example.com/a",
            draft_text="Hello world",
            archetype_disclosure="Meet Jana, a composite.",
            verdict="safe",
            verdict_reason="fine",
            classified_at=datetime.now(timezone.utc),
        )

    @patch("poster.linkedin_poster.create_post")
    @patch("poster.linkedin_poster.upload_image")
    @patch("poster.linkedin_poster.get_post_photo")
    def test_attaches_photo_and_appends_attribution_when_successful(self, mock_get_photo, mock_upload, mock_create):
        mock_get_photo.return_value = (b"jpeg-bytes", "image/jpeg", "Photo by Jane Doe on Unsplash (https://...)")
        mock_upload.return_value = "urn:li:image:xyz"
        mock_create.return_value = "urn:li:share:1"

        post_one(self._item(), "token", "urn:li:person:abc", unsplash_access_key="unsplash-key")

        mock_create.assert_called_once_with(
            "token",
            "urn:li:person:abc",
            "Hello world\n\nPhoto by Jane Doe on Unsplash (https://...)",
            image_urn="urn:li:image:xyz",
        )

    @patch("poster.linkedin_poster.create_post")
    @patch("poster.linkedin_poster.get_post_photo")
    def test_falls_back_to_text_only_when_photo_pick_fails(self, mock_get_photo, mock_create):
        mock_get_photo.side_effect = PhotoPickError("no results")
        mock_create.return_value = "urn:li:share:1"

        post_one(self._item(), "token", "urn:li:person:abc", unsplash_access_key="unsplash-key")

        mock_create.assert_called_once_with("token", "urn:li:person:abc", "Hello world", image_urn=None)

    @patch("poster.linkedin_poster.upload_image")
    @patch("poster.linkedin_poster.create_post")
    @patch("poster.linkedin_poster.get_post_photo")
    def test_falls_back_to_text_only_when_upload_fails(self, mock_get_photo, mock_create, mock_upload):
        mock_get_photo.return_value = (b"jpeg-bytes", "image/jpeg", "Photo by Jane Doe on Unsplash (https://...)")
        mock_upload.side_effect = requests.RequestException("network error")
        mock_create.return_value = "urn:li:share:1"

        post_one(self._item(), "token", "urn:li:person:abc", unsplash_access_key="unsplash-key")

        mock_create.assert_called_once_with("token", "urn:li:person:abc", "Hello world", image_urn=None)

    @patch("poster.linkedin_poster.create_post")
    @patch("poster.linkedin_poster.get_post_photo")
    def test_skips_photo_lookup_entirely_when_no_key_configured(self, mock_get_photo, mock_create):
        mock_create.return_value = "urn:li:share:1"

        post_one(self._item(), "token", "urn:li:person:abc", unsplash_access_key=None)

        mock_get_photo.assert_not_called()
        mock_create.assert_called_once_with("token", "urn:li:person:abc", "Hello world", image_urn=None)
