"""Unit tests for the Google Drive connector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aidomaincontext.connectors.google_drive import (
    GoogleDriveConnector,
    _extract_owner,
    _fetch_file_content,
    _refresh_token_if_needed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(
    *,
    file_id: str = "file_001",
    name: str = "Test Doc",
    mime_type: str = "application/vnd.google-apps.document",
    web_view_link: str = "https://docs.google.com/document/d/file_001",
    owners: list[dict] | None = None,
    modified_time: str = "2024-01-15T10:00:00Z",
) -> dict:
    if owners is None:
        owners = [{"emailAddress": "owner@example.com", "displayName": "Owner"}]
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "webViewLink": web_view_link,
        "owners": owners,
        "modifiedTime": modified_time,
    }


def _make_httpx_response(status_code: int, json_data: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"HTTP {status_code}",
                request=MagicMock(),
                response=resp,
            )
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# TestRefreshTokenIfNeeded
# ---------------------------------------------------------------------------


class TestRefreshTokenIfNeeded:
    @pytest.mark.asyncio
    async def test_valid_token_no_refresh(self):
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        config = {
            "access_token": "valid_token",
            "token_expiry": future_expiry,
            "refresh_token": "refresh_tok",
        }
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "valid_token"
        assert updates is None

    @pytest.mark.asyncio
    async def test_expired_token_triggers_refresh(self):
        past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config = {
            "access_token": "old_token",
            "token_expiry": past_expiry,
            "refresh_token": "refresh_tok",
        }
        mock_resp = _make_httpx_response(200, {"access_token": "new_token", "expires_in": 3600})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client),
            patch("aidomaincontext.config.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "client_id"
            mock_settings.google_oauth_client_secret = "client_secret"
            token, updates = await _refresh_token_if_needed(config, None)

        assert token == "new_token"
        assert updates is not None
        assert updates["access_token"] == "new_token"
        assert "token_expiry" in updates

    @pytest.mark.asyncio
    async def test_cursor_token_preferred_over_config(self):
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        config = {"access_token": "config_token", "token_expiry": future_expiry, "refresh_token": "r"}
        cursor = {"access_token": "cursor_token", "token_expiry": future_expiry}
        token, updates = await _refresh_token_if_needed(config, cursor)
        assert token == "cursor_token"
        assert updates is None

    @pytest.mark.asyncio
    async def test_no_refresh_token_skips_refresh(self):
        past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config = {"access_token": "tok", "token_expiry": past_expiry, "refresh_token": ""}
        token, updates = await _refresh_token_if_needed(config, None)
        assert token == "tok"
        assert updates is None

    @pytest.mark.asyncio
    async def test_invalid_expiry_string_triggers_refresh(self):
        config = {
            "access_token": "old_token",
            "token_expiry": "not-a-date",
            "refresh_token": "refresh_tok",
        }
        mock_resp = _make_httpx_response(200, {"access_token": "new_tok", "expires_in": 3600})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with (
            patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client),
            patch("aidomaincontext.config.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "cid"
            mock_settings.google_oauth_client_secret = "csec"
            token, updates = await _refresh_token_if_needed(config, None)

        assert token == "new_tok"
        assert updates is not None


# ---------------------------------------------------------------------------
# TestExtractOwner
# ---------------------------------------------------------------------------


class TestExtractOwner:
    def test_email_present(self):
        file = {"owners": [{"emailAddress": "owner@example.com", "displayName": "Owner"}]}
        assert _extract_owner(file) == "owner@example.com"

    def test_displayname_fallback(self):
        file = {"owners": [{"emailAddress": "", "displayName": "Jane Doe"}]}
        assert _extract_owner(file) == "Jane Doe"

    def test_empty_owners(self):
        assert _extract_owner({"owners": []}) is None

    def test_missing_owners_key(self):
        assert _extract_owner({}) is None


# ---------------------------------------------------------------------------
# TestFetchFileContent
# ---------------------------------------------------------------------------


class TestFetchFileContent:
    @pytest.mark.asyncio
    async def test_google_doc_export(self):
        resp = _make_httpx_response(200, text="Hello World")
        resp.text = "Hello World"
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_file_content(
            client, {}, "file_id", "application/vnd.google-apps.document"
        )
        assert result == "Hello World"
        call_kwargs = client.get.call_args
        assert "export" in call_kwargs[0][0]
        assert call_kwargs[1]["params"]["mimeType"] == "text/plain"

    @pytest.mark.asyncio
    async def test_spreadsheet_exported_as_csv(self):
        resp = _make_httpx_response(200, text="a,b,c")
        resp.text = "a,b,c"
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_file_content(
            client, {}, "sheet_id", "application/vnd.google-apps.spreadsheet"
        )
        assert result == "a,b,c"
        assert client.get.call_args[1]["params"]["mimeType"] == "text/csv"

    @pytest.mark.asyncio
    async def test_text_plain_download(self):
        resp = _make_httpx_response(200, text="plain text content")
        resp.text = "plain text content"
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_file_content(client, {}, "file_id", "text/plain")
        assert result == "plain text content"
        assert client.get.call_args[1]["params"] == {"alt": "media"}

    @pytest.mark.asyncio
    async def test_pdf_returns_none(self):
        client = AsyncMock()
        result = await _fetch_file_content(client, {}, "file_id", "application/pdf")
        assert result is None
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_export_4xx_returns_none(self):
        resp = _make_httpx_response(400)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        result = await _fetch_file_content(
            client, {}, "file_id", "application/vnd.google-apps.document"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_export_5xx_raises(self):
        resp = _make_httpx_response(500)
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_file_content(
                client, {}, "file_id", "application/vnd.google-apps.document"
            )


# ---------------------------------------------------------------------------
# TestValidateCredentials
# ---------------------------------------------------------------------------


class TestValidateCredentials:
    @pytest.mark.asyncio
    async def test_200_returns_true(self):
        config = {"access_token": "tok", "token_expiry": "", "refresh_token": ""}
        resp = _make_httpx_response(200, {"user": {}})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            connector = GoogleDriveConnector()
            result = await connector.validate_credentials(config)

        assert result is True

    @pytest.mark.asyncio
    async def test_401_returns_false(self):
        config = {"access_token": "bad_tok", "token_expiry": "", "refresh_token": ""}
        resp = _make_httpx_response(401)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            connector = GoogleDriveConnector()
            result = await connector.validate_credentials(config)

        assert result is False

    @pytest.mark.asyncio
    async def test_httpx_error_returns_false(self):
        config = {"access_token": "tok", "token_expiry": "", "refresh_token": ""}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("connection refused"))

        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            connector = GoogleDriveConnector()
            result = await connector.validate_credentials(config)

        assert result is False


# ---------------------------------------------------------------------------
# TestFetchAllFiles
# ---------------------------------------------------------------------------


class TestFetchAllFiles:
    def _make_config(self) -> dict:
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return {"access_token": "tok", "token_expiry": future_expiry, "refresh_token": "", "user_email": "user@example.com"}

    @pytest.mark.asyncio
    async def test_single_page_yields_docs(self):
        config = self._make_config()
        file = _make_file(file_id="doc1", name="My Doc")

        files_resp = _make_httpx_response(200, {"files": [file]})
        start_token_resp = _make_httpx_response(200, {"startPageToken": "tok123"})
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "document content"
        export_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        # startPageToken is fetched first, then files, then export
        mock_client.get = AsyncMock(side_effect=[start_token_resp, files_resp, export_resp])

        connector = GoogleDriveConnector()
        docs = []
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, cursor in connector.fetch_documents(config, None):
                docs.append(doc)
                cursors.append(cursor)

        assert len(docs) == 1
        assert docs[0].title == "My Doc"
        assert docs[0].source_id == "google_drive:user@example.com:doc1"
        assert docs[0].source_type == "google_drive_file"
        assert docs[0].metadata["drive_file_id"] == "doc1"

    @pytest.mark.asyncio
    async def test_skips_pdf_files(self):
        config = self._make_config()
        pdf_file = _make_file(file_id="pdf1", name="Report.pdf", mime_type="application/pdf")
        doc_file = _make_file(file_id="doc1", name="Notes.doc", mime_type="application/vnd.google-apps.document")

        files_resp = _make_httpx_response(200, {"files": [pdf_file, doc_file]})
        start_token_resp = _make_httpx_response(200, {"startPageToken": "tok_abc"})
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "notes content"
        export_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[start_token_resp, files_resp, export_resp])

        connector = GoogleDriveConnector()
        docs = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, _ in connector.fetch_documents(config, None):
                docs.append(doc)

        assert len(docs) == 1
        assert docs[0].source_id == "google_drive:user@example.com:doc1"

    @pytest.mark.asyncio
    async def test_pagination_across_two_pages(self):
        config = self._make_config()
        file1 = _make_file(file_id="f1", name="File 1")
        file2 = _make_file(file_id="f2", name="File 2")

        page1_resp = _make_httpx_response(200, {"files": [file1], "nextPageToken": "page2_tok"})
        export1_resp = MagicMock(spec=httpx.Response)
        export1_resp.status_code = 200
        export1_resp.text = "content 1"
        export1_resp.raise_for_status = MagicMock()

        page2_resp = _make_httpx_response(200, {"files": [file2]})
        export2_resp = MagicMock(spec=httpx.Response)
        export2_resp.status_code = 200
        export2_resp.text = "content 2"
        export2_resp.raise_for_status = MagicMock()

        start_token_resp = _make_httpx_response(200, {"startPageToken": "final_tok"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=[start_token_resp, page1_resp, export1_resp, page2_resp, export2_resp]
        )

        connector = GoogleDriveConnector()
        docs = []
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, cursor in connector.fetch_documents(config, None):
                docs.append(doc)
                cursors.append(cursor)

        assert len(docs) == 2
        assert cursors[-1]["changes_page_token"] == "final_tok"

    @pytest.mark.asyncio
    async def test_cursor_gets_changes_page_token_after_full_sync(self):
        config = self._make_config()
        file = _make_file()
        files_resp = _make_httpx_response(200, {"files": [file]})
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "content"
        export_resp.raise_for_status = MagicMock()
        start_token_resp = _make_httpx_response(200, {"startPageToken": "changes_tok_001"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        # startPageToken fetched first so every yielded cursor includes it
        mock_client.get = AsyncMock(side_effect=[start_token_resp, files_resp, export_resp])

        connector = GoogleDriveConnector()
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for _, cursor in connector.fetch_documents(config, None):
                cursors.append(cursor)

        assert cursors[-1]["changes_page_token"] == "changes_tok_001"

    @pytest.mark.asyncio
    async def test_none_content_file_skipped(self):
        config = self._make_config()
        unknown_file = _make_file(file_id="unk1", mime_type="application/octet-stream")

        files_resp = _make_httpx_response(200, {"files": [unknown_file]})
        start_token_resp = _make_httpx_response(200, {"startPageToken": "tok"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[start_token_resp, files_resp])

        connector = GoogleDriveConnector()
        docs = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, _ in connector.fetch_documents(config, None):
                docs.append(doc)

        assert docs == []

    @pytest.mark.asyncio
    async def test_owner_and_source_id_and_metadata(self):
        config = self._make_config()
        file = _make_file(
            file_id="xyz",
            name="Spec",
            mime_type="text/plain",
            owners=[{"emailAddress": "alice@corp.com"}],
            modified_time="2024-06-01T12:00:00Z",
        )

        files_resp = _make_httpx_response(200, {"files": [file]})
        download_resp = MagicMock(spec=httpx.Response)
        download_resp.status_code = 200
        download_resp.text = "spec text"
        download_resp.raise_for_status = MagicMock()
        start_token_resp = _make_httpx_response(200, {"startPageToken": "t"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[start_token_resp, files_resp, download_resp])

        connector = GoogleDriveConnector()
        docs = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, _ in connector.fetch_documents(config, None):
                docs.append(doc)

        assert len(docs) == 1
        doc = docs[0]
        assert doc.author == "alice@corp.com"
        assert doc.source_id == "google_drive:user@example.com:xyz"
        assert doc.metadata["mime_type"] == "text/plain"
        assert doc.metadata["modified_time"] == "2024-06-01T12:00:00Z"
        assert doc.metadata["drive_file_id"] == "xyz"


# ---------------------------------------------------------------------------
# TestFetchChanges
# ---------------------------------------------------------------------------


class TestFetchChanges:
    def _make_config(self) -> dict:
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return {
            "access_token": "tok",
            "token_expiry": future_expiry,
            "refresh_token": "",
            "user_email": "user@example.com",
        }

    def _make_cursor(self, token: str = "change_tok_1") -> dict:
        return {"changes_page_token": token}

    @pytest.mark.asyncio
    async def test_yields_changed_files(self):
        config = self._make_config()
        cursor = self._make_cursor()
        file = _make_file(file_id="c1", name="Changed Doc")
        change = {"removed": False, "file": file}

        changes_resp = _make_httpx_response(
            200, {"changes": [change], "newStartPageToken": "new_tok"}
        )
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "updated content"
        export_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[changes_resp, export_resp])

        connector = GoogleDriveConnector()
        docs = []
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, cur in connector.fetch_documents(config, cursor):
                docs.append(doc)
                cursors.append(cur)

        assert len(docs) == 1
        assert docs[0].title == "Changed Doc"
        assert cursors[-1]["changes_page_token"] == "new_tok"

    @pytest.mark.asyncio
    async def test_skips_removed_changes(self):
        config = self._make_config()
        cursor = self._make_cursor()
        removed_change = {"removed": True, "file": _make_file(file_id="del1")}

        changes_resp = _make_httpx_response(
            200, {"changes": [removed_change], "newStartPageToken": "new_tok"}
        )

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=changes_resp)

        connector = GoogleDriveConnector()
        docs = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, _ in connector.fetch_documents(config, cursor):
                docs.append(doc)

        assert docs == []

    @pytest.mark.asyncio
    async def test_updates_changes_page_token_to_new_start(self):
        # Verify that _fetch_changes updates new_cursor["changes_page_token"] to
        # newStartPageToken even when no docs are yielded (empty changes list).
        changes_resp = _make_httpx_response(
            200, {"changes": [], "newStartPageToken": "brand_new_tok"}
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=changes_resp)

        connector = GoogleDriveConnector()
        new_cursor: dict = {"changes_page_token": "old_tok"}
        async for _ in connector._fetch_changes(
            mock_client, {}, "user@example.com", "old_tok", new_cursor
        ):
            pass

        assert new_cursor["changes_page_token"] == "brand_new_tok"

    @pytest.mark.asyncio
    async def test_pagination_in_changes(self):
        config = self._make_config()
        cursor = self._make_cursor("start_tok")
        file1 = _make_file(file_id="c1")
        file2 = _make_file(file_id="c2")

        page1_resp = _make_httpx_response(
            200, {"changes": [{"removed": False, "file": file1}], "nextPageToken": "page2_tok"}
        )
        export1_resp = MagicMock(spec=httpx.Response)
        export1_resp.status_code = 200
        export1_resp.text = "content 1"
        export1_resp.raise_for_status = MagicMock()

        page2_resp = _make_httpx_response(
            200, {"changes": [{"removed": False, "file": file2}], "newStartPageToken": "end_tok"}
        )
        export2_resp = MagicMock(spec=httpx.Response)
        export2_resp.status_code = 200
        export2_resp.text = "content 2"
        export2_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[page1_resp, export1_resp, page2_resp, export2_resp])

        connector = GoogleDriveConnector()
        docs = []
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, cur in connector.fetch_documents(config, cursor):
                docs.append(doc)
                cursors.append(cur)

        assert len(docs) == 2
        assert cursors[-1]["changes_page_token"] == "end_tok"

    @pytest.mark.asyncio
    async def test_410_falls_back_to_full_sync(self):
        config = self._make_config()
        cursor = self._make_cursor("stale_tok")
        file = _make_file(file_id="full1", name="Full Sync Doc")

        gone_resp = _make_httpx_response(410)
        gone_resp.raise_for_status = MagicMock()
        gone_resp.status_code = 410

        files_resp = _make_httpx_response(200, {"files": [file]})
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "full sync content"
        export_resp.raise_for_status = MagicMock()
        start_token_resp = _make_httpx_response(200, {"startPageToken": "fresh_tok"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=[gone_resp, start_token_resp, files_resp, export_resp]
        )

        connector = GoogleDriveConnector()
        docs = []
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, cur in connector.fetch_documents(config, cursor):
                docs.append(doc)
                cursors.append(cur)

        assert len(docs) == 1
        assert docs[0].title == "Full Sync Doc"
        # After fallback full sync, the cursor should have the new changes_page_token
        assert cursors[-1]["changes_page_token"] == "fresh_tok"

    @pytest.mark.asyncio
    async def test_skips_unsupported_mime_in_changes(self):
        config = self._make_config()
        cursor = self._make_cursor()
        pdf_file = _make_file(file_id="pdf1", mime_type="application/pdf")
        change = {"removed": False, "file": pdf_file}

        changes_resp = _make_httpx_response(
            200, {"changes": [change], "newStartPageToken": "tok_next"}
        )

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=changes_resp)

        connector = GoogleDriveConnector()
        docs = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, _ in connector.fetch_documents(config, cursor):
                docs.append(doc)

        assert docs == []


# ---------------------------------------------------------------------------
# TestFetchDocumentsIntegration
# ---------------------------------------------------------------------------


class TestFetchDocumentsIntegration:
    def _make_config(self) -> dict:
        future_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return {
            "access_token": "integration_tok",
            "token_expiry": future_expiry,
            "refresh_token": "",
            "user_email": "integration@example.com",
        }

    @pytest.mark.asyncio
    async def test_no_cursor_triggers_full_sync(self):
        config = self._make_config()
        file = _make_file(file_id="int1", name="Integration Doc")

        files_resp = _make_httpx_response(200, {"files": [file]})
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "integration content"
        export_resp.raise_for_status = MagicMock()
        start_token_resp = _make_httpx_response(200, {"startPageToken": "int_tok"})

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[start_token_resp, files_resp, export_resp])

        connector = GoogleDriveConnector()
        docs = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, _ in connector.fetch_documents(config, None):
                docs.append(doc)

        assert len(docs) == 1
        assert docs[0].source_id == "google_drive:integration@example.com:int1"

    @pytest.mark.asyncio
    async def test_cursor_with_changes_page_token_triggers_incremental(self):
        config = self._make_config()
        cursor = {"changes_page_token": "incr_tok"}
        file = _make_file(file_id="int2", name="Incremental Doc")
        change = {"removed": False, "file": file}

        changes_resp = _make_httpx_response(
            200, {"changes": [change], "newStartPageToken": "next_incr_tok"}
        )
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "incremental content"
        export_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[changes_resp, export_resp])

        connector = GoogleDriveConnector()
        docs = []
        cursors = []
        with patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", return_value=mock_client):
            async for doc, cur in connector.fetch_documents(config, cursor):
                docs.append(doc)
                cursors.append(cur)

        assert len(docs) == 1
        assert cursors[-1]["changes_page_token"] == "next_incr_tok"

    @pytest.mark.asyncio
    async def test_token_refresh_propagated_in_cursor(self):
        past_expiry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config = {
            "access_token": "old_tok",
            "token_expiry": past_expiry,
            "refresh_token": "ref_tok",
            "user_email": "user@example.com",
        }

        refresh_resp = _make_httpx_response(200, {"access_token": "new_tok", "expires_in": 3600})
        file = _make_file(file_id="ref1")
        files_resp = _make_httpx_response(200, {"files": [file]})
        export_resp = MagicMock(spec=httpx.Response)
        export_resp.status_code = 200
        export_resp.text = "refreshed content"
        export_resp.raise_for_status = MagicMock()
        start_token_resp = _make_httpx_response(200, {"startPageToken": "st"})

        # Separate clients for refresh vs file fetching
        refresh_client = AsyncMock()
        refresh_client.__aenter__ = AsyncMock(return_value=refresh_client)
        refresh_client.__aexit__ = AsyncMock(return_value=False)
        refresh_client.post = AsyncMock(return_value=refresh_resp)

        files_client = AsyncMock()
        files_client.__aenter__ = AsyncMock(return_value=files_client)
        files_client.__aexit__ = AsyncMock(return_value=False)
        files_client.get = AsyncMock(side_effect=[start_token_resp, files_resp, export_resp])

        call_count = [0]

        def client_factory(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return refresh_client
            return files_client

        connector = GoogleDriveConnector()
        cursors = []
        with (
            patch("aidomaincontext.connectors.google_drive.httpx.AsyncClient", side_effect=client_factory),
            patch("aidomaincontext.config.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "cid"
            mock_settings.google_oauth_client_secret = "csec"
            async for _, cur in connector.fetch_documents(config, None):
                cursors.append(cur)

        assert cursors[-1]["access_token"] == "new_tok"
