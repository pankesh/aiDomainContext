"""Yahoo Mail connector — fetches emails via IMAP using an app-specific password.

Yahoo's Mail REST API is not available for new third-party developer apps.
This connector uses IMAP over SSL with an app-specific password instead.

Setup: https://help.yahoo.com/kb/generate-third-party-passwords-sln15241.html
"""

from __future__ import annotations

import email
import email.header
import html
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.message import Message as EmailMessage

import aioimaplib
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

_IMAP_HOST = "imap.mail.yahoo.com"
_IMAP_PORT = 993
_DEFAULT_FOLDER = "INBOX"
_FETCH_BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Body / header parsing helpers
# ---------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode HTML entities."""
    text = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded header value (e.g. =?UTF-8?b?...?=)."""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _extract_body(msg: EmailMessage) -> str:
    """Extract best plain-text body from an email.Message object."""
    plain_text = ""
    html_fallback = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain_text:
                plain_text = decoded
            elif ct == "text/html" and not html_fallback:
                html_fallback = _strip_html(decoded)
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_fallback = _strip_html(decoded)
            else:
                plain_text = decoded

    return plain_text.strip() or html_fallback.strip()


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
        """Attempt an IMAP login to verify credentials."""
        username = config.get("username", "")
        app_password = config.get("app_password", "")
        if not username or not app_password:
            return False
        try:
            client = aioimaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
            await client.wait_hello_from_server()
            resp = await client.login(username, app_password)
            await client.logout()
            return resp.result == "OK"
        except Exception:
            logger.exception("yahoo_mail.validate_credentials_failed")
            return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        username = config.get("username", "")
        app_password = config.get("app_password", "")
        folder = config.get("folder", _DEFAULT_FOLDER)
        last_uid: int = (cursor or {}).get("last_uid", 0)

        client = aioimaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
        await client.wait_hello_from_server()
        await client.login(username, app_password)
        try:
            await client.select(folder)

            # Fetch UIDs greater than last seen
            search_criterion = f"UID {last_uid + 1}:*" if last_uid else "ALL"
            _, data = await client.uid("search", search_criterion)
            uid_list = [int(u) for u in data[0].split() if u.strip().isdigit()]

            new_cursor: dict = dict(cursor or {})

            # Process in batches
            for i in range(0, len(uid_list), _FETCH_BATCH_SIZE):
                batch = uid_list[i : i + _FETCH_BATCH_SIZE]
                uid_range = ",".join(str(u) for u in batch)

                _, msg_data = await client.uid("fetch", uid_range, "(RFC822)")

                for j in range(0, len(msg_data), 2):
                    raw = msg_data[j]
                    if not isinstance(raw, bytes):
                        continue
                    try:
                        msg = email.message_from_bytes(raw)
                        uid = batch[j // 2]
                        doc = self._parse_message(msg, uid, username)
                        if doc:
                            if uid > new_cursor.get("last_uid", 0):
                                new_cursor["last_uid"] = uid
                            yield doc, {**new_cursor}
                    except Exception:
                        logger.exception("yahoo_mail.parse_failed", uid=batch[j // 2] if j // 2 < len(batch) else "?")
        finally:
            try:
                await client.logout()
            except Exception:
                pass

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_message(self, msg: EmailMessage, uid: int, username: str) -> DocumentBase | None:
        subject = _decode_header_value(msg.get("Subject", "") or "") or "(no subject)"
        from_header = _decode_header_value(msg.get("From", "") or "")
        date_header = msg.get("Date", "")
        message_id = msg.get("Message-ID", f"uid:{uid}").strip()

        body = _extract_body(msg)

        return DocumentBase(
            source_id=f"yahoo_mail:{username}:{uid}",
            source_type="yahoo_message",
            title=subject,
            content=body or subject,
            url="https://mail.yahoo.com/",
            author=from_header or None,
            metadata={
                "uid": uid,
                "message_id": message_id,
                "date": date_header,
            },
        )
