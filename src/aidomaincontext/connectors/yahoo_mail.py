"""Yahoo Mail connector — fetches emails via the Yahoo Mail REST API using OAuth 2.0."""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.connectors.retry import with_backoff
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

_YAHOO_MAIL_API = "https://mail.yahooapis.com/ws/mail/v3"
_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
_USERINFO_URL = "https://api.login.yahoo.com/openid/v1/userinfo"
_TOKEN_EXPIRY_BUFFER_SECONDS = 300  # refresh if expires within 5 minutes
_PAGE_SIZE = 50  # messages per page


# ---------------------------------------------------------------------------
# Body parsing helpers
# ---------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Remove HTML tags (including style/script block contents) and decode HTML entities."""
    text = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(parts: list[dict]) -> str:
    """Extract best plain-text body from Yahoo Mail message parts list."""
    plain_text = ""
    html_fallback = ""
    for part in parts:
        mime = part.get("mimeType", "")
        content = part.get("content", "")
        if not content:
            continue
        if mime == "text/plain":
            plain_text = content
        elif mime == "text/html" and not plain_text:
            html_fallback = _strip_html(content)
    return plain_text or html_fallback


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
        needs_refresh = bool(refresh_token)

    if not needs_refresh or not refresh_token:
        return access_token, None

    from aidomaincontext.config import settings  # noqa: PLC0415

    logger.info("yahoo_mail.refreshing_access_token")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "client_id": settings.yahoo_oauth_client_id,
                "client_secret": settings.yahoo_oauth_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "redirect_uri": settings.yahoo_oauth_redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        token_data = resp.json()

    new_access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)
    new_expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    logger.info("yahoo_mail.token_refreshed")
    return new_access_token, {"access_token": new_access_token, "token_expiry": new_expiry}


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


@register_connector
class YahooMailConnector:
    connector_type = "yahoo_mail"

    # ------------------------------------------------------------------
    # ConnectorProtocol
    # ------------------------------------------------------------------

    async def validate_credentials(self, config: dict) -> bool:
        access_token, _ = await _refresh_token_if_needed(config, None)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    _USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("yahoo_mail.validate_credentials_failed")
            return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        access_token, token_update = await _refresh_token_if_needed(config, cursor)
        user_email = config.get("user_email", "")
        user_id = config.get("user_id", "")
        last_sync_at: str | None = (cursor or {}).get("last_sync_at")

        new_cursor: dict = dict(cursor or {})
        if token_update:
            new_cursor.update(token_update)

        # Advance cursor timestamp for this run (fetch everything up to now)
        new_cursor["last_sync_at"] = datetime.now(timezone.utc).isoformat()

        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=30) as client:
            async for doc in self._fetch_messages(
                client, headers, user_id, user_email, last_sync_at, new_cursor
            ):
                yield doc, {**new_cursor}

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_messages(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        user_id: str,
        user_email: str,
        since: str | None,
        cursor: dict,
    ) -> AsyncIterator[DocumentBase]:
        """Paginate through inbox messages, optionally filtered by date."""
        encoded_user = quote(user_id, safe="")
        offset = 0

        # Convert ISO since-date to Unix milliseconds for the API filter
        since_ms: int | None = None
        if since:
            try:
                dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                since_ms = int(dt.timestamp() * 1000)
            except ValueError:
                pass

        while True:
            params: dict = {
                "fid": "Inbox",
                "count": _PAGE_SIZE,
                "offset": offset,
            }
            if since_ms is not None:
                params["since_date"] = since_ms

            resp = await with_backoff(
                lambda p=params: client.get(
                    f"{_YAHOO_MAIL_API}/{encoded_user}/messages",
                    headers=headers,
                    params=p,
                )
            )
            resp.raise_for_status()
            data = resp.json()

            messages = data.get("messages", [])
            for msg_stub in messages:
                mid = msg_stub.get("mid")
                if not mid:
                    continue
                doc = await self._fetch_message(client, headers, encoded_user, user_email, mid)
                if doc is not None:
                    yield doc

            # Stop when we've received fewer than a full page
            if len(messages) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

    async def _fetch_message(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        encoded_user: str,
        user_email: str,
        mid: str,
    ) -> DocumentBase | None:
        """Fetch and parse a single message by ID."""
        resp = await with_backoff(
            lambda: client.get(
                f"{_YAHOO_MAIL_API}/{encoded_user}/message/{mid}",
                headers=headers,
            )
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        msg = resp.json()
        subject = msg.get("subject") or "(no subject)"

        from_field = msg.get("from") or {}
        from_name = from_field.get("name", "")
        from_email = from_field.get("email", "")
        if from_name and from_email:
            author = f"{from_name} <{from_email}>"
        elif from_email:
            author = from_email
        elif from_name:
            author = from_name
        else:
            author = None

        # receivedDate is milliseconds since epoch
        received_ms = msg.get("receivedDate", 0)
        received_dt = datetime.fromtimestamp(received_ms / 1000, tz=timezone.utc).isoformat() if received_ms else ""

        parts = msg.get("parts", [])
        body = _extract_body(parts)

        flags = msg.get("flags", {})

        return DocumentBase(
            source_id=f"yahoo_mail:{user_email}:{mid}",
            source_type="yahoo_message",
            title=subject,
            content=body or subject,
            url="https://mail.yahoo.com/",
            author=author or None,
            metadata={
                "message_id": mid,
                "date": received_dt,
                "read": flags.get("read", False),
                "starred": flags.get("starred", False),
            },
        )
