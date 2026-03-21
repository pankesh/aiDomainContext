"""Google Drive connector — fetches files via the Drive REST API using OAuth 2.0."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

_DRIVE_API = "https://www.googleapis.com/drive/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TOKEN_EXPIRY_BUFFER_SECONDS = 300  # refresh if expires within 5 minutes

_FILES_QUERY = "mimeType != 'application/vnd.google-apps.folder' and trashed = false"

_EXPORT_MIME_MAP: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
_DOWNLOAD_MIME_TYPES = frozenset({"text/plain", "text/markdown", "text/csv"})
_SKIP_MIME_TYPES = frozenset({"application/vnd.google-apps.folder", "application/pdf"})


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


async def _refresh_token_if_needed(
    config: dict, cursor: dict | None
) -> tuple[str, dict | None]:
    """Return a valid access token, refreshing via refresh_token if needed.

    Returns (access_token, token_updates). token_updates is a non-None dict
    containing "access_token" and "token_expiry" when a refresh occurred,
    so the caller can persist them in the cursor.
    """
    # Prefer cursor-stored token (from a prior refresh) over config
    access_token = (cursor or {}).get("access_token") or config.get("access_token", "")
    token_expiry_str = (cursor or {}).get("token_expiry") or config.get("token_expiry", "")
    refresh_token = config.get("refresh_token", "")

    needs_refresh = False
    if token_expiry_str:
        try:
            expiry = datetime.fromisoformat(token_expiry_str.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            seconds_left = (expiry - datetime.now(timezone.utc)).total_seconds()
            if seconds_left < _TOKEN_EXPIRY_BUFFER_SECONDS:
                needs_refresh = True
        except ValueError:
            needs_refresh = True
    else:
        # No expiry stored — assume we need to refresh if we have a refresh_token
        needs_refresh = bool(refresh_token)

    if not needs_refresh or not refresh_token:
        return access_token, None

    # Lazy import to avoid circular import at module load time
    from aidomaincontext.config import settings  # noqa: PLC0415

    logger.info("google_drive.refreshing_access_token")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        token_data = resp.json()

    new_access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)
    new_expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    logger.info("google_drive.token_refreshed")
    return new_access_token, {"access_token": new_access_token, "token_expiry": new_expiry}


# ---------------------------------------------------------------------------
# File content helpers
# ---------------------------------------------------------------------------


def _extract_owner(file: dict) -> str | None:
    """Extract the owner email or display name from a Drive file object."""
    owners = file.get("owners", [])
    if not owners:
        return None
    return owners[0].get("emailAddress") or owners[0].get("displayName") or None


async def _fetch_file_content(
    client: httpx.AsyncClient,
    headers: dict,
    file_id: str,
    mime_type: str,
) -> str | None:
    """Download or export file content; returns None if the file should be skipped."""
    if mime_type in _EXPORT_MIME_MAP:
        target = _EXPORT_MIME_MAP[mime_type]
        resp = await client.get(
            f"{_DRIVE_API}/files/{file_id}/export",
            headers=headers,
            params={"mimeType": target},
        )
        try:
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                # e.g. export too large — silently skip
                logger.warning(
                    "google_drive.export_failed",
                    file_id=file_id,
                    status=exc.response.status_code,
                )
                return None
            raise

    if mime_type in _DOWNLOAD_MIME_TYPES:
        resp = await client.get(
            f"{_DRIVE_API}/files/{file_id}",
            headers=headers,
            params={"alt": "media"},
        )
        resp.raise_for_status()
        return resp.text

    # PDFs and other binary types — skip
    return None


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


@register_connector
class GoogleDriveConnector:
    connector_type = "google_drive"

    # ------------------------------------------------------------------
    # ConnectorProtocol
    # ------------------------------------------------------------------

    async def validate_credentials(self, config: dict) -> bool:
        access_token, _ = await _refresh_token_if_needed(config, None)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{_DRIVE_API}/about",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"fields": "user"},
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("google_drive.validate_credentials_failed")
            return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        access_token, token_update = await _refresh_token_if_needed(config, cursor)
        new_cursor: dict = dict(cursor or {})
        if token_update:
            new_cursor.update(token_update)

        headers = {"Authorization": f"Bearer {access_token}"}
        user_email = config.get("user_email", "unknown")

        async with httpx.AsyncClient(timeout=60) as client:
            if (cursor or {}).get("changes_page_token"):
                async for doc in self._fetch_changes(
                    client, headers, user_email, cursor["changes_page_token"], new_cursor
                ):
                    yield doc, {**new_cursor}
            else:
                async for doc in self._fetch_all_files(client, headers, user_email, new_cursor):
                    yield doc, {**new_cursor}

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        # Drive push channels not in scope
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_all_files(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        user_email: str,
        new_cursor: dict,
    ) -> AsyncIterator[DocumentBase]:
        """Full sync — paginate all non-folder, non-trashed files."""
        # Capture a changes cursor snapshot upfront so every yielded document
        # carries the token and the sync engine can resume incrementally if
        # the full sync is interrupted.
        token_resp = await client.get(
            f"{_DRIVE_API}/changes/startPageToken", headers=headers
        )
        token_resp.raise_for_status()
        new_cursor["changes_page_token"] = token_resp.json()["startPageToken"]

        page_token: str | None = None
        while True:
            params: dict = {
                "pageSize": 100,
                "q": _FILES_QUERY,
                "fields": "files(id,name,mimeType,webViewLink,owners,modifiedTime),nextPageToken",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await client.get(f"{_DRIVE_API}/files", headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            for file in data.get("files", []):
                file_id: str = file["id"]
                mime_type: str = file.get("mimeType", "")
                content = await _fetch_file_content(client, headers, file_id, mime_type)
                if content is None:
                    continue

                yield DocumentBase(
                    source_id=f"google_drive:{user_email}:{file_id}",
                    source_type="google_drive_file",
                    title=file.get("name") or "(untitled)",
                    content=content,
                    url=file.get("webViewLink"),
                    author=_extract_owner(file),
                    metadata={
                        "mime_type": mime_type,
                        "modified_time": file.get("modifiedTime"),
                        "drive_file_id": file_id,
                    },
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    async def _fetch_changes(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        user_email: str,
        page_token: str,
        new_cursor: dict,
    ) -> AsyncIterator[DocumentBase]:
        """Incremental sync using the Drive Changes API."""
        current_token = page_token
        while True:
            params: dict = {
                "pageToken": current_token,
                "includeRemoved": "false",
                "fields": "changes(file(id,name,mimeType,webViewLink,owners,modifiedTime),removed),newStartPageToken,nextPageToken",
            }

            resp = await client.get(f"{_DRIVE_API}/changes", headers=headers, params=params)

            if resp.status_code == 410:
                # Stale cursor — fall back to full sync
                logger.warning(
                    "google_drive.changes_token_expired",
                    action="falling_back_to_full_sync",
                )
                new_cursor.pop("changes_page_token", None)
                async for doc in self._fetch_all_files(client, headers, user_email, new_cursor):
                    yield doc
                return

            resp.raise_for_status()
            data = resp.json()

            next_page = data.get("nextPageToken")
            new_start = data.get("newStartPageToken")

            # Update the cursor before yielding docs from this page so that
            # every emitted (doc, cursor) snapshot is safe to resume from.
            if not next_page and new_start:
                new_cursor["changes_page_token"] = new_start

            for change in data.get("changes", []):
                if change.get("removed"):
                    continue
                file = change.get("file", {})
                file_id: str = file.get("id", "")
                mime_type: str = file.get("mimeType", "")
                if not file_id:
                    continue

                content = await _fetch_file_content(client, headers, file_id, mime_type)
                if content is None:
                    continue

                yield DocumentBase(
                    source_id=f"google_drive:{user_email}:{file_id}",
                    source_type="google_drive_file",
                    title=file.get("name") or "(untitled)",
                    content=content,
                    url=file.get("webViewLink"),
                    author=_extract_owner(file),
                    metadata={
                        "mime_type": mime_type,
                        "modified_time": file.get("modifiedTime"),
                        "drive_file_id": file_id,
                    },
                )

            if next_page:
                current_token = next_page
            else:
                break
