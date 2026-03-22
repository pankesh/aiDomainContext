"""Gmail connector — fetches emails via the Gmail REST API using OAuth 2.0."""

from __future__ import annotations

import base64
import html
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.connectors.retry import with_backoff
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TOKEN_EXPIRY_BUFFER_SECONDS = 300  # refresh if expires within 5 minutes


# ---------------------------------------------------------------------------
# Body / header parsing helpers
# ---------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Remove HTML tags (including style/script block contents) and decode HTML entities."""
    text = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_body(payload: dict) -> str:
    """Extract plain-text body from a Gmail message payload (recursive)."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            raw_html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            return _strip_html(raw_html)

    parts = payload.get("parts", [])
    plain_text = ""
    html_fallback = ""

    for part in parts:
        part_type = part.get("mimeType", "")
        if part_type == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                plain_text = base64.urlsafe_b64decode(data + "==").decode(
                    "utf-8", errors="replace"
                )
        elif part_type == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                raw_html = base64.urlsafe_b64decode(data + "==").decode(
                    "utf-8", errors="replace"
                )
                html_fallback = _strip_html(raw_html)
        elif part_type.startswith("multipart/"):
            nested = _parse_body(part)
            if nested:
                plain_text = nested

    return plain_text or html_fallback


def _get_header(headers: list[dict], name: str) -> str:
    """Extract a header value by name (case-insensitive)."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


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

    logger.info("gmail.refreshing_access_token")
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

    logger.info("gmail.token_refreshed")
    return new_access_token, {"access_token": new_access_token, "token_expiry": new_expiry}


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


@register_connector
class GmailConnector:
    connector_type = "gmail"

    # ------------------------------------------------------------------
    # ConnectorProtocol
    # ------------------------------------------------------------------

    async def validate_credentials(self, config: dict) -> bool:
        access_token, _ = await _refresh_token_if_needed(config, None)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{_GMAIL_API}/users/me/profile",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("gmail.validate_credentials_failed")
            return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        access_token, token_update = await _refresh_token_if_needed(config, cursor)
        user_email = config.get("user_email", "me")
        last_history_id: str | None = (cursor or {}).get("last_history_id")

        # Build mutable cursor; persist any refreshed token fields
        new_cursor: dict = dict(cursor or {})
        if token_update:
            new_cursor.update(token_update)

        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=30) as client:
            if last_history_id:
                async for doc in self._fetch_via_history(
                    client, headers, user_email, last_history_id, new_cursor
                ):
                    yield doc, {**new_cursor}
            else:
                async for doc in self._fetch_all_messages(
                    client, headers, user_email, new_cursor
                ):
                    yield doc, {**new_cursor}

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        # Pub/Sub webhooks are not implemented; return empty list
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_message(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        user_email: str,
        message_id: str,
        cursor: dict,
    ) -> DocumentBase | None:
        """Fetch and parse a single message; updates cursor['last_history_id'] in-place."""
        resp = await with_backoff(
            lambda: client.get(
                f"{_GMAIL_API}/users/me/messages/{message_id}",
                headers=headers,
                params={"format": "full"},
            )
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        msg = resp.json()
        payload_data = msg.get("payload", {})
        msg_headers = payload_data.get("headers", [])

        subject = _get_header(msg_headers, "Subject") or "(no subject)"
        from_header = _get_header(msg_headers, "From")
        date_header = _get_header(msg_headers, "Date")
        body = _parse_body(payload_data)

        thread_id = msg.get("threadId", "")
        labels = msg.get("labelIds", [])
        history_id: str | None = msg.get("historyId")

        # Advance the cursor to the highest history_id seen
        if history_id:
            current_max = int(cursor.get("last_history_id") or 0)
            if int(history_id) > current_max:
                cursor["last_history_id"] = history_id

        return DocumentBase(
            source_id=f"gmail:{user_email}:{message_id}",
            source_type="gmail_message",
            title=subject,
            content=body or subject,
            url=f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
            author=from_header or None,
            metadata={
                "labels": labels,
                "thread_id": thread_id,
                "date": date_header,
            },
        )

    async def _fetch_all_messages(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        user_email: str,
        cursor: dict,
    ) -> AsyncIterator[DocumentBase]:
        """Full sync — iterate over all messages in the mailbox."""
        page_token: str | None = None
        while True:
            params: dict = {"maxResults": 100}
            if page_token:
                params["pageToken"] = page_token

            resp = await with_backoff(
                lambda p=params: client.get(
                    f"{_GMAIL_API}/users/me/messages", headers=headers, params=p
                )
            )
            resp.raise_for_status()
            data = resp.json()

            for msg_stub in data.get("messages", []):
                doc = await self._fetch_message(
                    client, headers, user_email, msg_stub["id"], cursor
                )
                if doc is not None:
                    yield doc

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    async def _fetch_via_history(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        user_email: str,
        last_history_id: str,
        cursor: dict,
    ) -> AsyncIterator[DocumentBase]:
        """Incremental sync using the Gmail History API."""
        page_token: str | None = None
        while True:
            params: dict = {
                "startHistoryId": last_history_id,
                "historyTypes": "messageAdded",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await with_backoff(
                lambda p=params: client.get(
                    f"{_GMAIL_API}/users/me/history", headers=headers, params=p
                )
            )

            # 404 means historyId is too old — fall back to a full sync
            if resp.status_code == 404:
                logger.warning(
                    "gmail.history_id_expired",
                    last_history_id=last_history_id,
                    action="falling_back_to_full_sync",
                )
                async for doc in self._fetch_all_messages(client, headers, user_email, cursor):
                    yield doc
                return

            resp.raise_for_status()
            data = resp.json()

            for history_item in data.get("history", []):
                for msg_added in history_item.get("messagesAdded", []):
                    message_id = msg_added.get("message", {}).get("id")
                    if message_id:
                        doc = await self._fetch_message(
                            client, headers, user_email, message_id, cursor
                        )
                        if doc is not None:
                            yield doc

            page_token = data.get("nextPageToken")
            if not page_token:
                break
