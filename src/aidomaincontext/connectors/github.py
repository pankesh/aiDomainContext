"""GitHub connector — fetches issues, pull requests, and commits via the REST API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import structlog

from aidomaincontext.connectors.base import register_connector
from aidomaincontext.connectors.retry import with_backoff
from aidomaincontext.schemas.documents import DocumentBase

logger = structlog.get_logger()

_GITHUB_API = "https://api.github.com"
_PER_PAGE = 100
_MAX_RATE_LIMIT_RETRIES = 3


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _handle_rate_limit(response: httpx.Response) -> None:
    """Sleep until the rate-limit window resets, then return so the caller can retry."""
    reset_ts = response.headers.get("X-RateLimit-Reset")
    if reset_ts is not None:
        wait = max(int(reset_ts) - int(time.time()), 1)
    else:
        wait = 60
    logger.warning("github.rate_limited", wait_seconds=wait)
    await asyncio.sleep(wait)


async def _paginated_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
) -> AsyncIterator[dict]:
    """Yield every JSON object across all pages of a GitHub list endpoint."""
    params = dict(params or {})
    params.setdefault("per_page", _PER_PAGE)
    next_url: str | None = url

    while next_url is not None:
        retries = 0
        while True:
            cur_url = next_url
            cur_params = params if cur_url == url else None
            resp = await with_backoff(lambda u=cur_url, p=cur_params: client.get(u, params=p))

            if resp.status_code == 403 and retries < _MAX_RATE_LIMIT_RETRIES:
                await _handle_rate_limit(resp)
                retries += 1
                continue

            resp.raise_for_status()
            break

        items = resp.json()
        for item in items:
            yield item

        # Follow pagination via Link header
        next_url = None
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                # When following Link URLs the params are already embedded in the URL
                params = None
                break


async def _fetch_comments(client: httpx.AsyncClient, comments_url: str) -> str:
    """Return all comments on an issue/PR concatenated as text."""
    parts: list[str] = []
    async for comment in _paginated_get(client, comments_url):
        author = comment.get("user", {}).get("login", "unknown")
        body = comment.get("body") or ""
        parts.append(f"[{author}]: {body}")
    return "\n\n".join(parts)


async def _fetch_review_comments(client: httpx.AsyncClient, repo: str, pr_number: int) -> str:
    """Return all review comments on a pull request concatenated as text."""
    url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/comments"
    parts: list[str] = []
    async for comment in _paginated_get(client, url):
        author = comment.get("user", {}).get("login", "unknown")
        body = comment.get("body") or ""
        path = comment.get("path") or ""
        parts.append(f"[{author} on {path}]: {body}")
    return "\n\n".join(parts)


@register_connector
class GitHubConnector:
    connector_type = "github"

    # ------------------------------------------------------------------
    # ConnectorProtocol
    # ------------------------------------------------------------------

    async def validate_credentials(self, config: dict) -> bool:
        token = config.get("access_token", "")
        try:
            async with httpx.AsyncClient(headers=_headers(token), timeout=15) as client:
                resp = await client.get(f"{_GITHUB_API}/user")
                return resp.status_code == 200
        except httpx.HTTPError:
            logger.exception("github.validate_credentials_failed")
            return False

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]:
        token = config["access_token"]
        repos: list[str] = config.get("repos", [])
        since = (cursor or {}).get("last_sync_at")
        new_cursor = {"last_sync_at": _iso_now()}

        async with httpx.AsyncClient(headers=_headers(token), timeout=30) as client:
            for repo in repos:
                logger.info("github.sync_repo", repo=repo)

                async for doc in self._fetch_issues(client, repo, since):
                    yield doc, new_cursor

                async for doc in self._fetch_pull_requests(client, repo, since):
                    yield doc, new_cursor

                async for doc in self._fetch_commits(client, repo, since):
                    yield doc, new_cursor

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]:
        event = payload.get("event_type", "")
        body = payload.get("body", {})
        docs: list[DocumentBase] = []

        if event == "push":
            docs.extend(self._handle_push_event(body))
        elif event == "issues":
            doc = self._handle_issue_event(body)
            if doc is not None:
                docs.append(doc)
        elif event == "pull_request":
            doc = self._handle_pr_event(body)
            if doc is not None:
                docs.append(doc)
        else:
            logger.debug("github.webhook_ignored", event_type=event)

        return docs

    # ------------------------------------------------------------------
    # Fetchers (used by fetch_documents)
    # ------------------------------------------------------------------

    async def _fetch_issues(
        self, client: httpx.AsyncClient, repo: str, since: str | None
    ) -> AsyncIterator[DocumentBase]:
        url = f"{_GITHUB_API}/repos/{repo}/issues"
        params: dict[str, str] = {"state": "all", "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since

        async for item in _paginated_get(client, url, params):
            # The issues endpoint also returns pull requests; skip them here
            if "pull_request" in item:
                continue

            comments_text = ""
            if item.get("comments", 0) > 0:
                comments_text = await _fetch_comments(client, item["comments_url"])

            body = item.get("body") or ""
            content_parts = [item["title"], body]
            if comments_text:
                content_parts.append(comments_text)

            yield DocumentBase(
                source_id=f"github:{repo}:issue:{item['number']}",
                source_type="github_issue",
                title=item["title"],
                content="\n\n".join(content_parts),
                url=item["html_url"],
                author=item.get("user", {}).get("login"),
                metadata={
                    "repo": repo,
                    "state": item["state"],
                    "labels": [l["name"] for l in item.get("labels", [])],
                    "number": item["number"],
                },
            )

    async def _fetch_pull_requests(
        self, client: httpx.AsyncClient, repo: str, since: str | None
    ) -> AsyncIterator[DocumentBase]:
        url = f"{_GITHUB_API}/repos/{repo}/pulls"
        params: dict[str, str] = {"state": "all", "sort": "updated", "direction": "asc"}
        # The pulls endpoint does not support `since`, so we filter client-side
        # when doing incremental sync.

        async for item in _paginated_get(client, url, params):
            if since and item.get("updated_at", "") < since:
                continue

            body = item.get("body") or ""
            review_comments_text = await _fetch_review_comments(client, repo, item["number"])

            content_parts = [item["title"], body]
            if review_comments_text:
                content_parts.append(review_comments_text)

            yield DocumentBase(
                source_id=f"github:{repo}:pr:{item['number']}",
                source_type="github_pr",
                title=item["title"],
                content="\n\n".join(content_parts),
                url=item["html_url"],
                author=item.get("user", {}).get("login"),
                metadata={
                    "repo": repo,
                    "state": item["state"],
                    "labels": [l["name"] for l in item.get("labels", [])],
                    "number": item["number"],
                    "merged": item.get("merged_at") is not None,
                },
            )

    async def _fetch_commits(
        self, client: httpx.AsyncClient, repo: str, since: str | None
    ) -> AsyncIterator[DocumentBase]:
        url = f"{_GITHUB_API}/repos/{repo}/commits"
        params: dict[str, str] = {}
        if since:
            params["since"] = since

        async for item in _paginated_get(client, url, params):
            sha = item["sha"]
            commit = item.get("commit", {})
            message = commit.get("message", "")
            author = commit.get("author", {}).get("name", "unknown")

            # Fetch the individual commit for the file list
            file_summary = ""
            try:
                detail_resp = await client.get(f"{_GITHUB_API}/repos/{repo}/commits/{sha}")
                if detail_resp.status_code == 200:
                    files = detail_resp.json().get("files", [])
                    file_lines = [
                        f"  {f.get('status', '?')} {f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
                        for f in files
                    ]
                    if file_lines:
                        file_summary = "Files changed:\n" + "\n".join(file_lines)
            except httpx.HTTPError:
                logger.warning("github.commit_detail_failed", repo=repo, sha=sha)

            content_parts = [message]
            if file_summary:
                content_parts.append(file_summary)

            yield DocumentBase(
                source_id=f"github:{repo}:commit:{sha}",
                source_type="github_commit",
                title=message.split("\n", 1)[0][:200],
                content="\n\n".join(content_parts),
                url=item["html_url"],
                author=author,
                metadata={
                    "repo": repo,
                    "sha": sha,
                },
            )

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    def _handle_push_event(self, body: dict) -> list[DocumentBase]:
        repo = body.get("repository", {}).get("full_name", "")
        docs: list[DocumentBase] = []
        for commit in body.get("commits", []):
            sha = commit["id"]
            message = commit.get("message", "")
            files_added = commit.get("added", [])
            files_modified = commit.get("modified", [])
            files_removed = commit.get("removed", [])
            file_lines = (
                [f"  added {f}" for f in files_added]
                + [f"  modified {f}" for f in files_modified]
                + [f"  removed {f}" for f in files_removed]
            )
            file_summary = ""
            if file_lines:
                file_summary = "Files changed:\n" + "\n".join(file_lines)

            content_parts = [message]
            if file_summary:
                content_parts.append(file_summary)

            docs.append(
                DocumentBase(
                    source_id=f"github:{repo}:commit:{sha}",
                    source_type="github_commit",
                    title=message.split("\n", 1)[0][:200],
                    content="\n\n".join(content_parts),
                    url=commit.get("url", ""),
                    author=commit.get("author", {}).get("name"),
                    metadata={"repo": repo, "sha": sha},
                )
            )
        return docs

    def _handle_issue_event(self, body: dict) -> DocumentBase | None:
        issue = body.get("issue")
        if issue is None:
            return None
        repo = body.get("repository", {}).get("full_name", "")
        return DocumentBase(
            source_id=f"github:{repo}:issue:{issue['number']}",
            source_type="github_issue",
            title=issue["title"],
            content="\n\n".join([issue["title"], issue.get("body") or ""]),
            url=issue["html_url"],
            author=issue.get("user", {}).get("login"),
            metadata={
                "repo": repo,
                "state": issue["state"],
                "labels": [l["name"] for l in issue.get("labels", [])],
                "number": issue["number"],
            },
        )

    def _handle_pr_event(self, body: dict) -> DocumentBase | None:
        pr = body.get("pull_request")
        if pr is None:
            return None
        repo = body.get("repository", {}).get("full_name", "")
        return DocumentBase(
            source_id=f"github:{repo}:pr:{pr['number']}",
            source_type="github_pr",
            title=pr["title"],
            content="\n\n".join([pr["title"], pr.get("body") or ""]),
            url=pr["html_url"],
            author=pr.get("user", {}).get("login"),
            metadata={
                "repo": repo,
                "state": pr["state"],
                "labels": [l["name"] for l in pr.get("labels", [])],
                "number": pr["number"],
                "merged": pr.get("merged_at") is not None,
            },
        )
