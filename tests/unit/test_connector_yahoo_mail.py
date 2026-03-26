"""Unit tests for connectors/yahoo_mail.py (IMAP + app-specific password)."""

from __future__ import annotations

import email as email_lib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aidomaincontext.connectors.yahoo_mail import (
    YahooMailConnector,
    _decode_header_value,
    _extract_body,
    _strip_html,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plain_email(
    subject: str = "Test Subject",
    from_addr: str = "sender@example.com",
    body: str = "Hello, this is the email body.",
    date: str = "Mon, 01 Jan 2024 10:00:00 +0000",
) -> bytes:
    msg = email_lib.message_from_string(
        f"Subject: {subject}\r\n"
        f"From: {from_addr}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: <test-001@example.com>\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}"
    )
    return msg.as_bytes()


def _make_html_email(subject: str = "HTML Email", body_html: str = "<p>Hello <b>World</b></p>") -> bytes:
    msg = email_lib.message_from_string(
        f"Subject: {subject}\r\n"
        f"From: sender@example.com\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"\r\n"
        f"{body_html}"
    )
    return msg.as_bytes()


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

    def test_empty_string(self):
        assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# _decode_header_value
# ---------------------------------------------------------------------------


class TestDecodeHeaderValue:
    def test_plain_ascii(self):
        assert _decode_header_value("Hello World") == "Hello World"

    def test_empty_string(self):
        assert _decode_header_value("") == ""


# ---------------------------------------------------------------------------
# _extract_body
# ---------------------------------------------------------------------------


class TestExtractBody:
    def test_plain_text_message(self):
        raw = _make_plain_email(body="Plain text content")
        msg = email_lib.message_from_bytes(raw)
        assert _extract_body(msg) == "Plain text content"

    def test_html_message_strips_tags(self):
        raw = _make_html_email(body_html="<p>Hello <b>World</b></p>")
        msg = email_lib.message_from_bytes(raw)
        result = _extract_body(msg)
        assert "Hello" in result
        assert "World" in result
        assert "<p>" not in result

    def test_empty_body_returns_empty(self):
        msg = email_lib.message_from_string(
            "Subject: Test\r\nContent-Type: text/plain\r\n\r\n"
        )
        assert _extract_body(msg) == ""


# ---------------------------------------------------------------------------
# YahooMailConnector.validate_credentials
# ---------------------------------------------------------------------------


@pytest.fixture
def connector():
    return YahooMailConnector()


@pytest.fixture
def config():
    return {
        "username": "user@yahoo.com",
        "app_password": "xxxx xxxx xxxx xxxx",
        "folder": "INBOX",
    }


class TestValidateCredentials:
    @pytest.mark.asyncio
    async def test_successful_login_returns_true(self, connector, config):
        mock_client = AsyncMock()
        mock_client.wait_hello_from_server = AsyncMock()
        mock_client.login = AsyncMock(return_value=MagicMock(result="OK"))
        mock_client.logout = AsyncMock()

        with patch("aidomaincontext.connectors.yahoo_mail.aioimaplib.IMAP4_SSL", return_value=mock_client):
            result = await connector.validate_credentials(config)

        assert result is True

    @pytest.mark.asyncio
    async def test_failed_login_returns_false(self, connector, config):
        mock_client = AsyncMock()
        mock_client.wait_hello_from_server = AsyncMock()
        mock_client.login = AsyncMock(return_value=MagicMock(result="NO"))
        mock_client.logout = AsyncMock()

        with patch("aidomaincontext.connectors.yahoo_mail.aioimaplib.IMAP4_SSL", return_value=mock_client):
            result = await connector.validate_credentials(config)

        assert result is False

    @pytest.mark.asyncio
    async def test_connection_error_returns_false(self, connector, config):
        mock_client = AsyncMock()
        mock_client.wait_hello_from_server = AsyncMock(side_effect=ConnectionRefusedError("refused"))

        with patch("aidomaincontext.connectors.yahoo_mail.aioimaplib.IMAP4_SSL", return_value=mock_client):
            result = await connector.validate_credentials(config)

        assert result is False

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_false(self, connector):
        result = await connector.validate_credentials({})
        assert result is False


# ---------------------------------------------------------------------------
# YahooMailConnector._parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_plain_email_returns_correct_document(self, connector):
        raw = _make_plain_email(
            subject="Hello Tests",
            from_addr="bob@example.com",
            body="Unit test email body.",
        )
        msg = email_lib.message_from_bytes(raw)
        doc = connector._parse_message(msg, seq_num=42, username="user@yahoo.com")

        assert doc is not None
        assert doc.source_type == "yahoo_message"
        assert "user@yahoo.com" in doc.source_id
        assert doc.title == "Hello Tests"
        assert doc.author == "bob@example.com"
        assert "Unit test email body." in doc.content
        assert doc.metadata["seq_num"] == 42

    def test_no_subject_falls_back(self, connector):
        raw = _make_plain_email(subject="", body="body text")
        msg = email_lib.message_from_bytes(raw)
        doc = connector._parse_message(msg, seq_num=1, username="user@yahoo.com")
        assert doc is not None
        assert doc.title == "(no subject)"

    def test_html_body_gets_stripped(self, connector):
        raw = _make_html_email(body_html="<p>Hello <b>World</b></p>")
        msg = email_lib.message_from_bytes(raw)
        doc = connector._parse_message(msg, seq_num=2, username="user@yahoo.com")
        assert doc is not None
        assert "<p>" not in doc.content
        assert "Hello" in doc.content

    def test_empty_body_falls_back_to_subject(self, connector):
        raw = _make_plain_email(subject="Fallback Subject", body="")
        msg = email_lib.message_from_bytes(raw)
        doc = connector._parse_message(msg, seq_num=3, username="user@yahoo.com")
        assert doc is not None
        assert doc.content == "Fallback Subject"


# ---------------------------------------------------------------------------
# YahooMailConnector.handle_webhook
# ---------------------------------------------------------------------------


class TestHandleWebhook:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self, connector):
        result = await connector.handle_webhook({"event": "new_mail"})
        assert result == []
