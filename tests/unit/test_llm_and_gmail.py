"""Unit tests for generation/llm.py and connectors/gmail.py."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aidomaincontext.connectors.gmail import (
    GmailConnector,
    _get_header,
    _parse_body,
    _refresh_token_if_needed,
    _strip_html,
)
from aidomaincontext.generation.llm import _build_context, generate_answer, generate_answer_stream
from aidomaincontext.schemas.search import Message


# ================================================================== #
# Helpers
# ================================================================== #


def _b64(text: str) -> str:
    """URL-safe base64-encode a string (no padding — Gmail omits trailing =)."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _make_httpx_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://example.com"),
    )


def _build_mock_httpx_client(responses: dict[str, httpx.Response]) -> AsyncMock:
    """Return an AsyncMock for httpx.AsyncClient whose methods dispatch by URL substring."""

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


# ================================================================== #
# generation/llm.py — _build_context
# ================================================================== #


class TestBuildContext:
    def test_single_chunk_with_title(self):
        chunks = [{"title": "Onboarding Guide", "content": "Welcome to Acme Corp."}]
        result = _build_context(chunks)
        assert result == "[Source 1] (from: Onboarding Guide)\nWelcome to Acme Corp."

    def test_multiple_chunks_separated_by_divider(self):
        chunks = [
            {"title": "Doc A", "content": "Content A"},
            {"title": "Doc B", "content": "Content B"},
        ]
        result = _build_context(chunks)
        parts = result.split("\n\n---\n\n")
        assert len(parts) == 2
        assert parts[0] == "[Source 1] (from: Doc A)\nContent A"
        assert parts[1] == "[Source 2] (from: Doc B)\nContent B"

    def test_chunk_without_title_defaults_to_unknown(self):
        chunks = [{"content": "Some content without a title."}]
        result = _build_context(chunks)
        assert "(from: Unknown)" in result
        assert "Some content without a title." in result

    def test_empty_chunks_returns_empty_string(self):
        assert _build_context([]) == ""


# ================================================================== #
# generation/llm.py — generate_answer
# ================================================================== #


class TestGenerateAnswer:
    def _make_mock_client(self, answer_text: str) -> MagicMock:
        mock_content = MagicMock()
        mock_content.text = answer_text

        mock_response = MagicMock()
        mock_response.content = [mock_content]

        mock_messages = AsyncMock()
        mock_messages.create = AsyncMock(return_value=mock_response)

        mock_client = MagicMock()
        mock_client.messages = mock_messages
        return mock_client

    @pytest.mark.asyncio
    async def test_returns_tuple_of_str_and_list(self):
        mock_client = self._make_mock_client("Here is your answer.")
        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            answer, citations = await generate_answer("What is Acme?", [{"title": "T", "content": "C"}])
        assert isinstance(answer, str)
        assert isinstance(citations, list)

    @pytest.mark.asyncio
    async def test_citations_only_include_referenced_chunks(self):
        answer_text = "See [Source 1] for details. [Source 3] also relevant."
        chunks = [
            {"title": "Doc 1", "content": "Content 1", "url": "http://a.com"},
            {"title": "Doc 2", "content": "Content 2", "url": "http://b.com"},
            {"title": "Doc 3", "content": "Content 3", "url": "http://c.com"},
        ]
        mock_client = self._make_mock_client(answer_text)
        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            answer, citations = await generate_answer("query", chunks)

        assert answer == answer_text
        assert len(citations) == 2
        titles = {c.document_title for c in citations}
        assert titles == {"Doc 1", "Doc 3"}

    @pytest.mark.asyncio
    async def test_no_citations_when_none_referenced(self):
        answer_text = "I cannot find an answer in the provided context."
        chunks = [{"title": "Doc 1", "content": "Content 1"}]
        mock_client = self._make_mock_client(answer_text)
        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            _, citations = await generate_answer("query", chunks)
        assert citations == []

    @pytest.mark.asyncio
    async def test_history_turns_prepended_to_messages(self):
        mock_client = self._make_mock_client("Answer.")
        history = [
            Message(role="user", content="What is RAG?"),
            Message(role="assistant", content="RAG stands for..."),
        ]
        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            await generate_answer("Follow-up question?", [], history=history)

        call_kwargs = mock_client.messages.create.call_args
        messages_sent = call_kwargs.kwargs["messages"]

        # First two messages must be the history turns
        assert messages_sent[0] == {"role": "user", "content": "What is RAG?"}
        assert messages_sent[1] == {"role": "assistant", "content": "RAG stands for..."}
        # Last message is the current user query with RAG context
        assert messages_sent[-1]["role"] == "user"
        assert "Follow-up question?" in messages_sent[-1]["content"]

    @pytest.mark.asyncio
    async def test_no_history_sends_single_message(self):
        mock_client = self._make_mock_client("Answer.")
        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            await generate_answer("Solo question?", [])

        call_kwargs = mock_client.messages.create.call_args
        messages_sent = call_kwargs.kwargs["messages"]
        assert len(messages_sent) == 1
        assert messages_sent[0]["role"] == "user"
        assert "Solo question?" in messages_sent[0]["content"]

    @pytest.mark.asyncio
    async def test_citation_chunk_content_truncated_to_200_chars(self):
        long_content = "x" * 500
        answer_text = "See [Source 1]."
        chunks = [{"title": "Doc", "content": long_content, "url": None}]
        mock_client = self._make_mock_client(answer_text)
        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            _, citations = await generate_answer("query", chunks)
        assert len(citations) == 1
        assert len(citations[0].chunk_content) == 200


# ================================================================== #
# generation/llm.py — generate_answer_stream
# ================================================================== #


class TestGenerateAnswerStream:
    @pytest.mark.asyncio
    async def test_yields_text_chunks(self):
        text_chunks = ["Hello", " ", "world", "!"]

        # Build an async iterable for stream.text_stream
        async def _async_iter():
            for chunk in text_chunks:
                yield chunk

        mock_stream = AsyncMock()
        mock_stream.text_stream = _async_iter()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        mock_messages = MagicMock()
        mock_messages.stream = MagicMock(return_value=mock_stream)

        mock_client = MagicMock()
        mock_client.messages = mock_messages

        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            collected = []
            async for chunk in generate_answer_stream("query", [{"title": "T", "content": "C"}]):
                collected.append(chunk)

        assert collected == text_chunks

    @pytest.mark.asyncio
    async def test_stream_with_empty_chunks(self):
        async def _async_iter():
            return
            yield  # make it an async generator

        mock_stream = AsyncMock()
        mock_stream.text_stream = _async_iter()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        mock_messages = MagicMock()
        mock_messages.stream = MagicMock(return_value=mock_stream)

        mock_client = MagicMock()
        mock_client.messages = mock_messages

        with patch("aidomaincontext.generation.llm._get_client", return_value=mock_client):
            collected = [chunk async for chunk in generate_answer_stream("query", [])]

        assert collected == []


# ================================================================== #
# connectors/gmail.py — _strip_html
# ================================================================== #


class TestStripHtml:
    def test_removes_basic_tags(self):
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_strips_style_block_contents(self):
        text = "<style>body { color: red; }</style><p>Visible</p>"
        result = _strip_html(text)
        assert "color" not in result
        assert "Visible" in result

    def test_strips_script_block_contents(self):
        text = "<script>alert('xss')</script><p>Safe</p>"
        result = _strip_html(text)
        assert "alert" not in result
        assert "Safe" in result

    def test_decodes_html_entities(self):
        assert _strip_html("&amp; &lt; &gt; &quot;") == "& < > \""

    def test_decodes_nbsp(self):
        result = _strip_html("Hello&nbsp;World")
        # Non-breaking space decoded then collapsed to a regular space
        assert "Hello" in result
        assert "World" in result

    def test_collapses_whitespace(self):
        result = _strip_html("<p>  lots   of   space  </p>")
        assert "  " not in result

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_plain_text_unchanged(self):
        assert _strip_html("No tags here") == "No tags here"


# ================================================================== #
# connectors/gmail.py — _parse_body
# ================================================================== #


class TestParseBody:
    def test_text_plain_returns_decoded_body(self):
        text = "Hello from plain text."
        payload = {"mimeType": "text/plain", "body": {"data": _b64(text)}}
        assert _parse_body(payload) == text

    def test_text_html_strips_tags(self):
        html = "<p>Hello from <b>HTML</b>.</p>"
        payload = {"mimeType": "text/html", "body": {"data": _b64(html)}}
        result = _parse_body(payload)
        assert "Hello from" in result
        assert "<p>" not in result
        assert "<b>" not in result

    def test_multipart_prefers_plain_over_html(self):
        plain_text = "Plain version"
        html_text = "<p>HTML version</p>"
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(plain_text)}},
                {"mimeType": "text/html", "body": {"data": _b64(html_text)}},
            ],
        }
        result = _parse_body(payload)
        assert result == plain_text

    def test_multipart_falls_back_to_html_when_no_plain(self):
        html_text = "<p>HTML only</p>"
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64(html_text)}},
            ],
        }
        result = _parse_body(payload)
        assert "HTML only" in result
        assert "<p>" not in result

    def test_nested_multipart_recurses(self):
        plain_text = "Deep plain text"
        inner = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(plain_text)}},
            ],
        }
        outer = {"mimeType": "multipart/mixed", "parts": [inner]}
        result = _parse_body(outer)
        assert result == plain_text

    def test_missing_body_data_returns_empty(self):
        payload = {"mimeType": "text/plain", "body": {}}
        assert _parse_body(payload) == ""

    def test_unknown_mimetype_with_no_parts_returns_empty(self):
        payload = {"mimeType": "application/octet-stream", "body": {"data": _b64("binary")}}
        assert _parse_body(payload) == ""


# ================================================================== #
# connectors/gmail.py — _get_header
# ================================================================== #


class TestGetHeader:
    def _headers(self):
        return [
            {"name": "Subject", "value": "Hello World"},
            {"name": "From", "value": "alice@example.com"},
            {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
        ]

    def test_found_returns_value(self):
        assert _get_header(self._headers(), "Subject") == "Hello World"

    def test_case_insensitive_lookup(self):
        assert _get_header(self._headers(), "subject") == "Hello World"
        assert _get_header(self._headers(), "SUBJECT") == "Hello World"
        assert _get_header(self._headers(), "fRoM") == "alice@example.com"

    def test_not_found_returns_empty_string(self):
        assert _get_header(self._headers(), "X-Custom-Header") == ""

    def test_empty_headers_list(self):
        assert _get_header([], "Subject") == ""


# ================================================================== #
# connectors/gmail.py — _refresh_token_if_needed
# ================================================================== #


class TestRefreshTokenIfNeeded:
    @pytest.mark.asyncio
    async def test_no_expiry_no_refresh_token_returns_token_as_is(self):
        config = {"access_token": "tok123"}
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "tok123"
        assert updates is None

    @pytest.mark.asyncio
    async def test_token_still_valid_no_refresh(self):
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        config = {
            "access_token": "valid_tok",
            "token_expiry": future_expiry,
            "refresh_token": "reftok",
        }
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "valid_tok"
        assert updates is None

    @pytest.mark.asyncio
    async def test_expired_token_calls_httpx_and_returns_new_token(self):
        past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config = {
            "access_token": "old_tok",
            "token_expiry": past_expiry,
            "refresh_token": "reftok",
            "client_id": "cid",
            "client_secret": "csec",
        }

        token_response = _make_httpx_response(
            {"access_token": "new_tok", "expires_in": 3600}
        )

        mock_client = _build_mock_httpx_client({"oauth2.googleapis.com": token_response})

        with patch("aidomaincontext.connectors.gmail.httpx.AsyncClient", return_value=mock_client):
            with patch("aidomaincontext.config.settings") as mock_settings:
                mock_settings.google_oauth_client_id = "cid"
                mock_settings.google_oauth_client_secret = "csec"
                token, updates = await _refresh_token_if_needed(config, None)

        assert token == "new_tok"
        assert updates is not None
        assert updates["access_token"] == "new_tok"
        assert "token_expiry" in updates

    @pytest.mark.asyncio
    async def test_invalid_expiry_string_triggers_refresh(self):
        config = {
            "access_token": "tok",
            "token_expiry": "not-a-date",
            "refresh_token": "reftok",
        }

        token_response = _make_httpx_response(
            {"access_token": "refreshed_tok", "expires_in": 3600}
        )
        mock_client = _build_mock_httpx_client({"oauth2.googleapis.com": token_response})

        with patch("aidomaincontext.connectors.gmail.httpx.AsyncClient", return_value=mock_client):
            with patch("aidomaincontext.config.settings") as mock_settings:
                mock_settings.google_oauth_client_id = "cid"
                mock_settings.google_oauth_client_secret = "csec"
                token, updates = await _refresh_token_if_needed(config, None)

        assert token == "refreshed_tok"
        assert updates is not None

    @pytest.mark.asyncio
    async def test_cursor_token_takes_precedence_over_config_token(self):
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        config = {"access_token": "config_tok", "token_expiry": future_expiry}
        cursor = {"access_token": "cursor_tok", "token_expiry": future_expiry}
        token, updates = await _refresh_token_if_needed(config, cursor)
        assert token == "cursor_tok"
        assert updates is None

    @pytest.mark.asyncio
    async def test_no_refresh_token_skips_refresh_even_when_expired(self):
        past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config = {"access_token": "old_tok", "token_expiry": past_expiry}
        # No refresh_token key — should NOT call httpx
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "old_tok"
        assert updates is None


# ================================================================== #
# connectors/gmail.py — GmailConnector.validate_credentials
# ================================================================== #


@pytest.fixture
def gmail_connector():
    return GmailConnector()


@pytest.fixture
def gmail_config():
    return {
        "access_token": "ya29.test_token",
        "token_expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "user_email": "alice@example.com",
    }


class TestValidateCredentials:
    @pytest.mark.asyncio
    async def test_200_returns_true(self, gmail_connector, gmail_config):
        profile_response = _make_httpx_response(
            {"emailAddress": "alice@example.com", "messagesTotal": 100}
        )
        mock_client = _build_mock_httpx_client({"/profile": profile_response})

        with patch("aidomaincontext.connectors.gmail.httpx.AsyncClient", return_value=mock_client):
            result = await gmail_connector.validate_credentials(gmail_config)

        assert result is True

    @pytest.mark.asyncio
    async def test_non_200_returns_false(self, gmail_connector, gmail_config):
        error_response = _make_httpx_response({"error": "unauthorized"}, status_code=401)
        mock_client = _build_mock_httpx_client({"/profile": error_response})

        with patch("aidomaincontext.connectors.gmail.httpx.AsyncClient", return_value=mock_client):
            result = await gmail_connector.validate_credentials(gmail_config)

        assert result is False

    @pytest.mark.asyncio
    async def test_httpx_exception_returns_false(self, gmail_connector, gmail_config):
        async def _raise_get(url, *, headers=None, params=None):
            raise httpx.ConnectError("connection refused")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_raise_get)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("aidomaincontext.connectors.gmail.httpx.AsyncClient", return_value=mock_client):
            result = await gmail_connector.validate_credentials(gmail_config)

        assert result is False


# ================================================================== #
# connectors/gmail.py — GmailConnector._fetch_message
# ================================================================== #


class TestFetchMessage:
    def _message_payload(
        self,
        message_id: str = "msg001",
        thread_id: str = "thread001",
        history_id: str = "12345",
        subject: str = "Test Subject",
        from_addr: str = "bob@example.com",
        date: str = "Mon, 01 Jan 2024 10:00:00 +0000",
        body_text: str = "Hello, this is the email body.",
        labels: list[str] | None = None,
    ) -> dict:
        return {
            "id": message_id,
            "threadId": thread_id,
            "historyId": history_id,
            "labelIds": labels or ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": from_addr},
                    {"name": "Date", "value": date},
                ],
                "body": {"data": _b64(body_text)},
            },
        }

    @pytest.mark.asyncio
    async def test_404_returns_none(self, gmail_connector):
        not_found = _make_httpx_response({}, status_code=404)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=not_found)
        cursor: dict = {}

        result = await gmail_connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "alice@example.com",
            "msg_missing",
            cursor,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_200_returns_document_base_with_correct_fields(self, gmail_connector):
        msg = self._message_payload(
            message_id="msg001",
            thread_id="thr001",
            history_id="99",
            subject="Hello Tests",
            from_addr="bob@example.com",
            body_text="Unit test email body.",
        )
        ok_response = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_response)
        cursor: dict = {}

        doc = await gmail_connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "alice@example.com",
            "msg001",
            cursor,
        )

        assert doc is not None
        assert doc.source_id == "gmail:alice@example.com:msg001"
        assert doc.source_type == "gmail_message"
        assert doc.title == "Hello Tests"
        assert doc.author == "bob@example.com"
        assert doc.url == "https://mail.google.com/mail/u/0/#inbox/msg001"
        assert "Unit test email body." in doc.content
        assert doc.metadata["thread_id"] == "thr001"
        assert "INBOX" in doc.metadata["labels"]

    @pytest.mark.asyncio
    async def test_200_updates_cursor_last_history_id(self, gmail_connector):
        msg = self._message_payload(message_id="msg002", history_id="500")
        ok_response = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_response)
        cursor: dict = {}

        await gmail_connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "alice@example.com",
            "msg002",
            cursor,
        )

        assert cursor["last_history_id"] == "500"

    @pytest.mark.asyncio
    async def test_cursor_history_id_only_advances(self, gmail_connector):
        """last_history_id should only be updated when the new id is higher."""
        msg = self._message_payload(message_id="msg003", history_id="100")
        ok_response = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_response)
        cursor: dict = {"last_history_id": "999"}

        await gmail_connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "alice@example.com",
            "msg003",
            cursor,
        )

        # Cursor had 999; message has 100 — should NOT regress
        assert cursor["last_history_id"] == "999"

    @pytest.mark.asyncio
    async def test_no_subject_falls_back_to_no_subject(self, gmail_connector):
        msg = {
            "id": "msg_nosub",
            "threadId": "thr",
            "historyId": "1",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"data": _b64("body")},
            },
        }
        ok_response = _make_httpx_response(msg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=ok_response)

        doc = await gmail_connector._fetch_message(
            mock_client,
            {"Authorization": "Bearer tok"},
            "alice@example.com",
            "msg_nosub",
            {},
        )

        assert doc is not None
        assert doc.title == "(no subject)"
