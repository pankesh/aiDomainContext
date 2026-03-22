"""Jira connector — fetches issues and comments via the Jira REST API v3."""

from __future__ import annotations

import base64
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

_PER_PAGE = 100


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _headers(email: str, api_token: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _base_url(domain: str) -> str:
    domain = domain.rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return domain


def _issue_to_document(issue: dict, domain: str) -> DocumentBase:
    """Convert a Jira issue dict to a DocumentBase."""
    key = issue["key"]
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    description = (fields.get("renderedFields") or fields).get("description") or ""

    # Collect comments
    comment_parts: list[str] = []
    comments_data = fields.get("comment", {}).get("comments", [])
    for comment in comments_data:
        author = comment.get("author", {}).get("displayName", "unknown")
        body = (comment.get("renderedBody") or comment.get("body")) or ""
        comment_parts.append(f"[{author}]: {body}")

    content_parts = [summary]
    if description:
        content_parts.append(description)
    if comment_parts:
        content_parts.append("\n\n".join(comment_parts))

    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    author = reporter.get("displayName") or reporter.get("emailAddress")

    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    project_key = (fields.get("project") or {}).get("key", "")

    base = _base_url(domain)

    return DocumentBase(
        source_id=f"jira:{domain}:{key}",
        source_type="jira_issue",
        title=summary,
        content="\n\n".join(content_parts),
        url=f"{base}/browse/{key}",
        author=author,
        metadata={
            "domain": domain,
            "issue_key": key,
            "project_key": project_key,
            "status": status,
            "priority": priority,
            "issue_type": issue_type,
            "assignee": assignee.get("displayName"),
        },
    )


@register_connector
class JiraConnector:
    connector_type = "jira"

    # ------------------------------------------------------------------
    # ConnectorProtocol
    # ------------------------------------------------------------------

    async def validate_credentials(self, config: dict) -> bool:
        email = config.get("email", "")
        api_token = config.get("api_token", "")
        domain = config.get("domain", "")
        if not all([email, api_token, domain]):
            return False

        base = _base_url(domain)
        try:
            async with httpx.AsyncClient(
                headers=_headers(email, api_token), timeout=15
            ) as client:
                resp = await client.get(f"{base}/rest/api/3/myself")
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("jira.validate_credentials_failed")
            return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        email = config["email"]
        api_token = config["api_token"]
        domain = config["domain"]
        project_keys: list[str] = config.get("project_keys", [])
        last_sync_at = (cursor or {}).get("last_sync_at")
        new_cursor = {"last_sync_at": _iso_now()}

        base = _base_url(domain)
        jql = self._build_jql(project_keys, last_sync_at)

        async with httpx.AsyncClient(
            headers=_headers(email, api_token), timeout=30
        ) as client:
            start_at = 0
            while True:
                params = {
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": _PER_PAGE,
                    "expand": "renderedFields,comments",
                    "fields": (
                        "summary,description,comment,status,priority,"
                        "issuetype,project,assignee,reporter,renderedFields"
                    ),
                }
                resp = await client.get(f"{base}/rest/api/3/search", params=params)
                resp.raise_for_status()
                data = resp.json()

                issues = data.get("issues", [])
                if not issues:
                    break

                for issue in issues:
                    doc = _issue_to_document(issue, domain)
                    yield doc, new_cursor

                start_at += len(issues)
                if start_at >= data.get("total", 0):
                    break

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        """Handle Jira webhook events (issue_created, issue_updated)."""
        event = payload.get("webhookEvent", "")
        issue = payload.get("issue")
        domain = payload.get("_domain", "")

        if issue is None:
            logger.debug("jira.webhook_no_issue", webhook_event=event)
            return []

        if event not in ("jira:issue_created", "jira:issue_updated"):
            logger.debug("jira.webhook_ignored", webhook_event=event)
            return []

        doc = _issue_to_document(issue, domain)
        return [doc]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_jql(self, project_keys: list[str], last_sync_at: str | None) -> str:
        conditions: list[str] = []
        if project_keys:
            safe_keys = [k for k in project_keys if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,9}", k)]
            if safe_keys:
                keys_str = ", ".join(f'"{k}"' for k in safe_keys)
                conditions.append(f"project in ({keys_str})")
        if last_sync_at:
            # JQL expects format: "YYYY-MM-DD HH:MM"
            try:
                dt = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))
                jql_date = dt.strftime("%Y-%m-%d %H:%M")
                conditions.append(f'updated >= "{jql_date}"')
            except ValueError:
                logger.warning("jira.invalid_cursor_date", last_sync_at=last_sync_at)
        where = " AND ".join(conditions)
        return f"{where} ORDER BY updated ASC" if where else "ORDER BY updated ASC"
