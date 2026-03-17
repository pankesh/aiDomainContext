"""Canned GitHub API response payloads for unit tests."""

ISSUES_LIST_RESPONSE = [
    {
        "number": 42,
        "title": "Bug: login page crashes on Safari",
        "body": "When opening the login page on Safari 17, the page shows a white screen.",
        "state": "open",
        "html_url": "https://github.com/acme/webapp/issues/42",
        "comments": 1,
        "comments_url": "https://api.github.com/repos/acme/webapp/issues/42/comments",
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "updated_at": "2024-03-01T12:00:00Z",
    },
    {
        # This item has a pull_request key and should be skipped by _fetch_issues
        "number": 43,
        "title": "feat: add dark mode",
        "body": "Adds dark mode support.",
        "state": "open",
        "html_url": "https://github.com/acme/webapp/pull/43",
        "pull_request": {"url": "https://api.github.com/repos/acme/webapp/pulls/43"},
        "comments": 0,
        "comments_url": "https://api.github.com/repos/acme/webapp/issues/43/comments",
        "user": {"login": "bob"},
        "labels": [],
        "updated_at": "2024-03-01T11:00:00Z",
    },
]

ISSUE_COMMENTS_RESPONSE = [
    {
        "id": 9001,
        "body": "I can reproduce this on Safari 17.2. Looks like a WebKit regression.",
        "user": {"login": "charlie"},
        "created_at": "2024-03-01T13:00:00Z",
    },
]

PULLS_LIST_RESPONSE = [
    {
        "number": 50,
        "title": "Refactor authentication module",
        "body": "Split the auth module into smaller files for better maintainability.",
        "state": "closed",
        "merged_at": "2024-02-28T09:00:00Z",
        "html_url": "https://github.com/acme/webapp/pull/50",
        "user": {"login": "dana"},
        "labels": [{"name": "refactor"}],
        "updated_at": "2024-03-01T10:00:00Z",
    },
    {
        "number": 51,
        "title": "Add rate limiting to API",
        "body": "Implements token-bucket rate limiting for all API endpoints.",
        "state": "open",
        "merged_at": None,
        "html_url": "https://github.com/acme/webapp/pull/51",
        "user": {"login": "eve"},
        "labels": [{"name": "enhancement"}],
        "updated_at": "2024-03-02T08:00:00Z",
    },
]

PR_REVIEW_COMMENTS_RESPONSE = [
    {
        "id": 7001,
        "body": "Nit: this constant should be uppercase.",
        "path": "src/auth/config.py",
        "user": {"login": "frank"},
        "created_at": "2024-02-27T14:00:00Z",
    },
]

COMMITS_LIST_RESPONSE = [
    {
        "sha": "abc123def456",
        "html_url": "https://github.com/acme/webapp/commit/abc123def456",
        "commit": {
            "message": "fix(auth): handle expired refresh tokens\n\nCloses #42",
            "author": {"name": "alice", "date": "2024-03-01T12:30:00Z"},
        },
    },
    {
        "sha": "789ghi012jkl",
        "html_url": "https://github.com/acme/webapp/commit/789ghi012jkl",
        "commit": {
            "message": "chore: update dependencies",
            "author": {"name": "bot", "date": "2024-03-01T06:00:00Z"},
        },
    },
]

COMMIT_DETAIL_RESPONSE = {
    "sha": "abc123def456",
    "files": [
        {
            "filename": "src/auth/refresh.py",
            "status": "modified",
            "additions": 12,
            "deletions": 3,
        },
        {
            "filename": "tests/test_auth.py",
            "status": "modified",
            "additions": 25,
            "deletions": 0,
        },
    ],
}

# ------------------------------------------------------------------ #
# Webhook payloads
# ------------------------------------------------------------------ #

GITHUB_PUSH_EVENT = {
    "event_type": "push",
    "body": {
        "ref": "refs/heads/main",
        "repository": {"full_name": "acme/webapp"},
        "commits": [
            {
                "id": "aaa111bbb222",
                "message": "feat: add user avatars",
                "url": "https://github.com/acme/webapp/commit/aaa111bbb222",
                "author": {"name": "alice"},
                "added": ["src/avatars.py"],
                "modified": ["src/models/user.py"],
                "removed": [],
            },
            {
                "id": "ccc333ddd444",
                "message": "test: avatar upload tests",
                "url": "https://github.com/acme/webapp/commit/ccc333ddd444",
                "author": {"name": "alice"},
                "added": ["tests/test_avatars.py"],
                "modified": [],
                "removed": [],
            },
        ],
    },
}

GITHUB_ISSUES_EVENT = {
    "event_type": "issues",
    "body": {
        "action": "opened",
        "issue": {
            "number": 99,
            "title": "Feature request: export to CSV",
            "body": "It would be great to export search results as CSV.",
            "state": "open",
            "html_url": "https://github.com/acme/webapp/issues/99",
            "user": {"login": "grace"},
            "labels": [{"name": "enhancement"}],
        },
        "repository": {"full_name": "acme/webapp"},
    },
}

GITHUB_PR_EVENT = {
    "event_type": "pull_request",
    "body": {
        "action": "opened",
        "pull_request": {
            "number": 60,
            "title": "Add CSV export endpoint",
            "body": "Implements CSV export for search results. Closes #99.",
            "state": "open",
            "merged_at": None,
            "html_url": "https://github.com/acme/webapp/pull/60",
            "user": {"login": "grace"},
            "labels": [{"name": "enhancement"}],
        },
        "repository": {"full_name": "acme/webapp"},
    },
}
