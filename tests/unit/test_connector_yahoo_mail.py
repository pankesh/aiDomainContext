"""Unit tests for connectors/yahoo_mail.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aidomaincontext.connectors.yahoo_mail import (
    YahooMailConnector,
    _extract_body,
    _refresh_token_if_needed,
    _strip_html,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_httpx_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://example.com"),
    )


def _build_mock_httpx_client(responses: dict[str, httpx.Response]) -> AsyncMock:
    """Return an AsyncMock for httpx.AsyncClient dispatching by URL substring."""

    async def _get(url, *, headers=None, params=None):
        for key, resp in responses.items():
            if key in str(url):
                return resp
        raise AssertionError(f"Unexpected GET URL: {url}")

    async def _post(url, *, data=None, json=None, headers=None):
        for key, resp in responses.items():
            if key in str(url):
                return resp
        raise AssertionError(f"Unexpected POST URL: {url}")

    client = AsyncMock()
    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _future_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _past_expiry() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


class TestStripHtml:
    def test_removes_basic_tags(self):
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_strips_style_block(self):
        result = _strip_html("<style>body{color:red}</style><p>Visible</p>")
        assert "color" not in result
        assert "Visible" in result

    def test_decodes_html_entities(self):
        assert _strip_html("&amp; &lt; &gt;") == "& < >"

    def test_collapses_whitespace(self):
        result = _strip_html("<p>  lots   of   space  </p>")
        assert "  " not in result

    def test_empty_string(self):
        assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# _extract_body
# ---------------------------------------------------------------------------


class TestExtractBody:
    def test_prefers_plain_over_html(self):
        parts = [
            {"mimeType": "text/plain", "content": "Plain text"},
            {"mimeType": "text/html", "content": "<p>HTML text</p>"},
        ]
        assert _extract_body(parts) == "Plain text"

    def test_falls_back_to_html_when_no_plain(self):
        parts = [{"mimeType": "text/html", "content": "<p>HTML only</p>"}]
        result = _extract_body(parts)
        assert "HTML only" in result
        assert "<p>" not in result

    def test_returns_empty_when_no_parts(self):
        assert _extract_body([]) == ""

    def test_skips_parts_with_empty_content(self):
        parts = [{"mimeType": "text/plain", "content": ""}]
        assert _extract_body(parts) == ""


# ---------------------------------------------------------------------------
# _refresh_token_if_needed
# ---------------------------------------------------------------------------


class TestRefreshTokenIfNeeded:
    @pytest.mark.asyncio
    async def test_valid_token_no_refresh(self):
        config = {
            "access_token": "valid_tok",
            "token_expiry": _future_expiry(),
            "refresh_token": "reftok",
        }
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "valid_tok"
        assert updates is None

    @pytest.mark.asyncio
    async def test_no_expiry_no_refresh_token_returns_as_is(self):
        config = {"access_token": "tok123"}
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "tok123"
        assert updates is None

    @pytest.mark.asyncio
    async def test_expired_token_calls_yahoo_and_returns_new_token(self):
        config = {
            "access_token": "old_tok",
            "token_expiry": _past_expiry(),
            "refresh_token": "reftok",
        }
        token_resp = _make_httpx_response({"access_token": "new_tok", "expires_in": 3600})
        mock_client = _build_mock_httpx_client({"login.yahoo.com": token_resp})

        with patch("aidomaincontext.connectors.yahoo_mail.httpx.AsyncClient", return_value=mock_client):
            with patch("aidomaincontext.config.settings") as mock_settings:
                mock_settings.yahoo_oauth_client_id = "cid"
                mock_settings.yahoo_oauth_client_secret = "csec"
                mock_settings.yahoo_oauth_redirect_uri = "http://localhost/callback"
                token, updates = await _refresh_token_if_needed(config, None)

        assert token == "new_tok"
        assert updates is not None
        assert updates["access_token"] == "new_tok"
        assert "token_expiry" in updates

    @pytest.mark.asyncio
    async def test_cursor_token_takes_precedence_over_config_token(self):
        config = {"access_token": "config_tok", "token_expiry": _future_expiry()}
        cursor = {"access_token": "cursor_tok", "token_expiry": _future_expiry()}
        token, updates = await _refresh_token_if_needed(config, cursor)
        assert token == "cursor_tok"
        assert updates is None

    @pytest.mark.asyncio
    async def test_no_refresh_token_skips_refresh_when_expired(self):
        config = {"access_token": "old_tok", "token_expiry": _past_expiry()}
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "old_tok"
        assert updates is None

    @pytest.mark.asyncio
    async def test_invalid_expiry_triggers_refresh(self):
        config = {
            "access_token": "tok",
            "token_expiry": "not-a-date",
            "refresh_token": "reftok",
        }
        token_resp = _make_httpx_response({"access_token": "refreshed_tok", "expires_in": 3600})
        mock_client = _build_mock_httpx_client({"login.yahoo.com": token_resp})

        with patch("aidomaincontext.connectors.yahoo_mail.httpx.AsyncClient", return_value=mock_client):
            with patch("aidomaincontext.config.settings") as mock_settings:
                mock_settings.yahoo_oauth_client_id = "cid"
                mock_settings.yahoo_oauth_client_secret = "csec"
                mock_settings.yahoo_oauth_redirect_uri = "http://localhost/callback"
                token, updates = await _refresh_token_if_needed(config, None)

        assert token == "refreshed_tok"


# ---------------------------------------------------------------------------
# YahooMailConnector.validate_credentials
# ---------------------------------------------------------------------------


@pytest.fixture
def connector():
    return YahooMailConnector()


@pytest.fixture
def config():
    return {
        "access_token": "ya_test_token",
        "token_expiry": _future_expiry(),
        "user_email": "user@yahoo.com",
        "user_id": "YAHOO_USER_123",
    }


class TestValidateCredentials:
    @pytest.mark.asyncio
    async def test_200_returns_true(self, connector, config):
        userinfo_resp = _make_httpx_response({"sub": "123", "email": "user@yahoo.com"})
        mock_client = _build_mock_httpx_client({"userinfo": userinfo_resp})

        with patch("aidomaincontext.connectors.yahoo_mail.httpx.AsyncClient", return_value=mock_client):
            result = await connector.validate_credentials(config)

        assert result is True

    @pytest.mark.asyncio
    async def test_401_returns_false(self, connector, config):
        error_resp = _make_httpx_response({"error": "unauthorized"}, status_code=401)
        mock_client = _build_mock_httpx_client({"userinfo": error_resp})

        with patch("aidomaincontext.connectors.yahoo_mail.httpx.AsyncClient", return_value=mock_client):
            result = await connector.validate_credentials(config)

        assert result is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self, connector, config):
        async def _raise_get(url, *, headers=None, params=None):
            raise httpx.ConnectError("connection refused")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_raise_get)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("aidomaincontext.connectors.yahoo_mail.httpx.AsyncClient", return_value=mock_client):
            result = await connector.validate_credentials(config)

        assert result is False


# ---------------------------------------------------------------------------
# YahooMailConnector._fetch_message
# ---------------------------------------------------------------------------


class TestFetchMessage:
    def _message_payload(
        self,
        mid: str = "msg001",
        subject: str = "Test Subject",
        from_email: str = "bob@example.com",
        from_name: str = "Bob",
        body: str = "Hello, this is the email body.",
        received_ms: int = 1700000000000,
    ) -> dict:
        return {
            "mid": mid,
            "subject": subject,
            "from": {"email": from_email, "name": from_name},
            "receivedDate": received_ms,
            "flags": {"read": False, "starred": False},
            "parts": [
                {"mimeType": "text/plain", "content": body},
            ],
        }

    @pytest.mark.asyncio
    async def test_404_returns_none(self, connector):
        not_found = _make_httpx_response({}, status_code=404)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=not_found)

        result = await connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "YAHOO_USER_123",
            "user@yahoo.com",
            "msg_missing",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_200_returns_correct_document(self, connector):
        msg = self._message_payload(
            mid="msg001",
            subject="Hello Tests",
            from_email="bob@example.com",
            from_name="Bob",
            body="Unit test email body.",
        )
        ok_resp = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_resp)

        doc = await connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "YAHOO_USER_123",
            "user@yahoo.com",
            "msg001",
        )

        assert doc is not None
        assert doc.source_id == "yahoo_mail:user@yahoo.com:msg001"
        assert doc.source_type == "yahoo_message"
        assert doc.title == "Hello Tests"
        assert doc.author == "Bob <bob@example.com>"
        assert "Unit test email body." in doc.content
        assert doc.metadata["message_id"] == "msg001"

    @pytest.mark.asyncio
    async def test_no_subject_falls_back_to_no_subject(self, connector):
        msg = {
            "mid": "msg_nosub",
            "subject": "",
            "from": {"email": "x@y.com", "name": ""},
            "receivedDate": 0,
            "flags": {},
            "parts": [],
        }
        ok_resp = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_resp)

        doc = await connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "YAHOO_USER_123",
            "user@yahoo.com",
            "msg_nosub",
        )
        assert doc is not None
        assert doc.title == "(no subject)"

    @pytest.mark.asyncio
    async def test_html_only_body_gets_stripped(self, connector):
        msg = {
            "mid": "msg_html",
            "subject": "HTML Email",
            "from": {"email": "x@y.com", "name": ""},
            "receivedDate": 1700000000000,
            "flags": {},
            "parts": [
                {"mimeType": "text/html", "content": "<p>Hello <b>World</b></p>"},
            ],
        }
        ok_resp = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_resp)

        doc = await connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "YAHOO_USER_123",
            "user@yahoo.com",
            "msg_html",
        )
        assert doc is not None
        assert "<p>" not in doc.content
        assert "Hello" in doc.content
        assert "World" in doc.content


# ---------------------------------------------------------------------------
# YahooMailConnector.handle_webhook
# ---------------------------------------------------------------------------


class TestHandleWebhook:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self, connector):
        result = await connector.handle_webhook({"event": "message_created"})
        assert result == []
