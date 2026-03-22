"""Slack connector — fetches messages from Slack channels via the Web API."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.connectors.retry import with_backoff
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

SLACK_BASE_URL = "https://slack.com/api"

# Slack tier-1 rate limit budget: stay well under 1 req/sec per method.
_DEFAULT_PAGE_LIMIT = 200
_REPLY_PAGE_LIMIT = 200


async def _slack_request(
    client: httpx.AsyncClient,
    method: str,
    token: str,
    params: dict | None = None,
) -> dict:
    """Make a Slack Web API call with automatic retry on 429 rate-limit responses."""
    url = f"{SLACK_BASE_URL}/{method}"
    headers = {"Authorization": f"Bearer {token}"}

    resp = await with_backoff(lambda: client.get(url, headers=headers, params=params or {}))
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        logger.error("slack_api_error", method=method, error=error)
        raise RuntimeError(f"Slack API error: {error}")

    return data


def _message_to_document(
    msg: dict,
    channel_id: str,
    channel_name: str,
    thread_text: str | None = None,
    reply_count: int = 0,
) -> DocumentBase:
    """Convert a single Slack message dict into a DocumentBase."""
    text = msg.get("text", "")
    full_content = text
    if thread_text:
        full_content = f"{text}\n\n--- Thread Replies ---\n{thread_text}"

    ts = msg["ts"]
    # Deep-link: Slack uses the ts without the dot in its archive URLs.
    ts_link = ts.replace(".", "")

    return DocumentBase(
        source_id=f"slack:{channel_id}:{ts}",
        source_type="slack_message",
        title=text[:100] if text else "(empty message)",
        content=full_content,
        url=f"https://slack.com/archives/{channel_id}/p{ts_link}",
        author=msg.get("user"),
        metadata={
            "channel": channel_id,
            "channel_name": channel_name,
            "thread_ts": msg.get("thread_ts", ts),
            "reply_count": reply_count,
        },
    )


async def _fetch_thread_replies(
    client: httpx.AsyncClient,
    token: str,
    channel_id: str,
    thread_ts: str,
) -> list[dict]:
    """Fetch all reply messages for a given thread, excluding the parent."""
    replies: list[dict] = []
    cursor: str | None = None

    while True:
        params: dict = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": _REPLY_PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor

        data = await _slack_request(client, "conversations.replies", token, params)

        messages = data.get("messages", [])
        # The first message in the response is the parent; skip it.
        for msg in messages:
            if msg["ts"] != thread_ts:
                replies.append(msg)

        meta = data.get("response_metadata", {})
        cursor = meta.get("next_cursor")
        if not cursor:
            break

    return replies


@register_connector
class SlackConnector:
    """Connector for ingesting messages from Slack workspaces."""

    connector_type = "slack"

    # --------------------------------------------------------------------- #
    # ConnectorProtocol methods
    # --------------------------------------------------------------------- #

    async def validate_credentials(self, config: dict) -> bool:
        """Verify the bot token by calling ``auth.test``."""
        token = config.get("bot_token", "")
        if not token:
            return False

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                data = await _slack_request(client, "auth.test", token)
                logger.info(
                    "slack_auth_ok",
                    team=data.get("team"),
                    user=data.get("user"),
                )
                return True
            except Exception:
                logger.exception("slack_auth_failed")
                return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        """Yield ``(DocumentBase, updated_cursor)`` for every message since *last_sync_ts*.

        If ``config["channels"]`` is ``None`` (or absent), all public channels
        the bot can see are fetched.  Otherwise only the listed channel IDs.
        """
        token: str = config["bot_token"]
        channel_ids: list[str] | None = config.get("channels")
        oldest: str = (cursor or {}).get("last_sync_ts", "0")

        async with httpx.AsyncClient(timeout=30) as client:
            channels = await self._resolve_channels(client, token, channel_ids)

            latest_ts = oldest
            for ch_id, ch_name in channels:
                async for doc, msg_ts in self._fetch_channel_messages(
                    client, token, ch_id, ch_name, oldest
                ):
                    if msg_ts > latest_ts:
                        latest_ts = msg_ts
                    yield doc, {"last_sync_ts": latest_ts}

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        """Handle an inbound Slack Events API ``message`` event.

        Expected *payload* shape (outer envelope already unwrapped)::

            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "channel": "C123",
                    "text": "hello",
                    "user": "U456",
                    "ts": "1234567890.123456",
                    ...
                },
                "token": "...",       # verification token (legacy)
                "team_id": "T789",
            }
        """
        event = payload.get("event", {})
        event_type = event.get("type")

        if event_type != "message":
            logger.debug("slack_webhook_ignored", event_type=event_type)
            return []

        # Ignore bot messages, message_changed sub-types, etc.
        if event.get("subtype"):
            logger.debug("slack_webhook_subtype_ignored", subtype=event["subtype"])
            return []

        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")

        doc = _message_to_document(
            msg=event,
            channel_id=channel_id,
            channel_name=event.get("channel_name", channel_id),
            reply_count=0,
        )

        # If this message is a reply inside a thread, note it in metadata.
        if thread_ts and thread_ts != ts:
            doc.metadata["thread_ts"] = thread_ts

        logger.info(
            "slack_webhook_document",
            source_id=doc.source_id,
            channel=channel_id,
        )
        return [doc]

    # --------------------------------------------------------------------- #
    # Internal helpers
    # --------------------------------------------------------------------- #

    async def _resolve_channels(
        self,
        client: httpx.AsyncClient,
        token: str,
        channel_ids: list[str] | None,
    ) -> list[tuple[str, str]]:
        """Return ``[(channel_id, channel_name), ...]``.

        When *channel_ids* is ``None`` all public channels are discovered via
        ``conversations.list``.
        """
        if channel_ids is not None:
            # Fetch channel info for each explicitly requested channel so we
            # can include the human-readable name.
            results: list[tuple[str, str]] = []
            for ch_id in channel_ids:
                data = await _slack_request(
                    client,
                    "conversations.info",
                    token,
                    {"channel": ch_id},
                )
                name = data.get("channel", {}).get("name", ch_id)
                results.append((ch_id, name))
            return results

        # Discover all public channels the bot has access to.
        channels: list[tuple[str, str]] = []
        cursor: str | None = None

        while True:
            params: dict = {
                "types": "public_channel",
                "exclude_archived": "true",
                "limit": _DEFAULT_PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor

            data = await _slack_request(client, "conversations.list", token, params)

            for ch in data.get("channels", []):
                channels.append((ch["id"], ch.get("name", ch["id"])))

            meta = data.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor:
                break

        logger.info("slack_channels_resolved", count=len(channels))
        return channels

    async def _fetch_channel_messages(
        self,
        client: httpx.AsyncClient,
        token: str,
        channel_id: str,
        channel_name: str,
        oldest: str,
    ) -> AsyncIterator[tuple[DocumentBase, str]]:
        """Yield ``(DocumentBase, message_ts)`` for every top-level message in a channel."""
        api_cursor: str | None = None

        while True:
            params: dict = {
                "channel": channel_id,
                "limit": _DEFAULT_PAGE_LIMIT,
                "oldest": oldest,
            }
            if api_cursor:
                params["cursor"] = api_cursor

            data = await _slack_request(
                client, "conversations.history", token, params
            )

            for msg in data.get("messages", []):
                # Skip non-user messages (join/leave/bot, etc.)
                if msg.get("subtype"):
                    continue

                reply_count = msg.get("reply_count", 0)
                thread_text: str | None = None

                if reply_count > 0:
                    replies = await _fetch_thread_replies(
                        client, token, channel_id, msg["ts"]
                    )
                    thread_text = "\n".join(
                        r.get("text", "") for r in replies if r.get("text")
                    )

                doc = _message_to_document(
                    msg,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    thread_text=thread_text,
                    reply_count=reply_count,
                )

                yield doc, msg["ts"]

            meta = data.get("response_metadata", {})
            api_cursor = meta.get("next_cursor")
            if not api_cursor:
                break

        logger.debug(
            "slack_channel_fetched",
            channel=channel_id,
            channel_name=channel_name,
        )
