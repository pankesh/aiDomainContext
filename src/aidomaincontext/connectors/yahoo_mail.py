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
        import asyncio  # noqa: PLC0415

        username = config.get("username", "")
        app_password = config.get("app_password", "")
        if not username or not app_password:
            return False
        try:
            async with asyncio.timeout(15):
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
        last_sync_at: str | None = (cursor or {}).get("last_sync_at")

        client = aioimaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
        await client.wait_hello_from_server()
        await client.login(username, app_password)
        try:
            await client.select(folder)

            # Build search criterion — SINCE <date> for incremental, ALL for full
            if last_sync_at:
                try:
                    dt = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))
                    # IMAP SINCE uses DD-Mon-YYYY format
                    since_str = dt.strftime("%d-%b-%Y")
                    search_criterion = f'SINCE "{since_str}"'
                except ValueError:
                    search_criterion = "ALL"
            else:
                search_criterion = "ALL"

            _, data = await client.search(search_criterion, charset=None)
            raw = data[0] if isinstance(data[0], str) else data[0].decode()
            seq_list = [s for s in raw.split() if s.strip().isdigit()]

            new_cursor: dict = dict(cursor or {})
            new_cursor["last_sync_at"] = datetime.now(timezone.utc).isoformat()

            # Process in batches
            for i in range(0, len(seq_list), _FETCH_BATCH_SIZE):
                batch = seq_list[i : i + _FETCH_BATCH_SIZE]
                seq_range = ",".join(batch)

                _, msg_data = await client.fetch(seq_range, "(RFC822)")

                seq_num = int(batch[0])
                for item in msg_data:
                    if not isinstance(item, bytes):
                        continue
                    try:
                        msg = email.message_from_bytes(item)
                        doc = self._parse_message(msg, seq_num, username)
                        if doc:
                            yield doc, {**new_cursor}
                        seq_num += 1
                    except Exception:
                        logger.exception("yahoo_mail.parse_failed", seq=seq_num)
                        seq_num += 1
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

    def _parse_message(self, msg: EmailMessage, seq_num: int, username: str) -> DocumentBase | None:
        subject = _decode_header_value(msg.get("Subject", "") or "") or "(no subject)"
        from_header = _decode_header_value(msg.get("From", "") or "")
        date_header = msg.get("Date", "")
        message_id = msg.get("Message-ID", f"seq:{seq_num}").strip()

        body = _extract_body(msg)

        # Use Message-ID as the stable unique key for source_id
        stable_id = message_id.strip("<>") or f"seq:{seq_num}"

        return DocumentBase(
            source_id=f"yahoo_mail:{username}:{stable_id}",
            source_type="yahoo_message",
            title=subject,
            content=body or subject,
            url="https://mail.yahoo.com/",
            author=from_header or None,
            metadata={
                "seq_num": seq_num,
                "message_id": message_id,
                "date": date_header,
            },
        )
