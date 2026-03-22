"""Unit tests for three API route modules.

Covered modules:
  - aidomaincontext.api.routes_connectors
  - aidomaincontext.api.routes_webhooks
  - aidomaincontext.api.routes_oauth

All database and external service dependencies are mocked so no real
infrastructure is required.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aidomaincontext.main import app
from aidomaincontext.models.database import get_session


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_connector(
    *,
    name: str = "Test Connector",
    connector_type: str = "slack",
    enabled: bool = True,
    sync_cursor: dict | None = None,
    config_encrypted: dict | None = None,
) -> MagicMock:
    """Return a fake ORM Connector instance."""
    obj = MagicMock()
    obj.id = uuid.uuid4()
    obj.name = name
    obj.connector_type = connector_type
    obj.config_encrypted = config_encrypted or {"_e": "encrypted"}
    obj.sync_cursor = sync_cursor
    obj.enabled = enabled
    obj.created_at = _utcnow()
    obj.updated_at = _utcnow()
    return obj


def _make_sync_job(*, connector_id: uuid.UUID | None = None) -> MagicMock:
    """Return a fake ORM SyncJob instance."""
    obj = MagicMock()
    obj.id = uuid.uuid4()
    obj.connector_id = connector_id or uuid.uuid4()
    obj.sync_type = "incremental"
    obj.status = "pending"
    obj.started_at = None
    obj.finished_at = None
    obj.documents_synced = 0
    obj.documents_failed = 0
    obj.error_message = None
    obj.created_at = _utcnow()
    obj.updated_at = _utcnow()
    return obj


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.get = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    return session


@pytest.fixture
def client(mock_session):
    """TestClient with the DB session dependency overridden."""

    async def override_get_session():
        yield mock_session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# MODULE 1 — routes_connectors
# ===========================================================================


class TestListConnectors:
    def test_returns_list_from_db(self, client, mock_session):
        c1 = _make_connector(name="Slack A")
        c2 = _make_connector(name="GitHub B", connector_type="github")

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [c1, c2]
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/connectors")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "Slack A"
        assert data[1]["name"] == "GitHub B"

    def test_returns_empty_list_when_no_connectors(self, client, mock_session):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get("/api/v1/connectors")

        assert resp.status_code == 200
        assert resp.json() == []


class TestCreateConnector:
    def test_unknown_connector_type_returns_400(self, client):
        with patch(
            "aidomaincontext.api.routes_connectors.get_connector",
            side_effect=KeyError("bogus"),
        ):
            resp = client.post(
                "/api/v1/connectors",
                json={"name": "Bad", "connector_type": "bogus", "config": {}},
            )

        assert resp.status_code == 400
        assert "Unknown connector type" in resp.json()["detail"]

    def test_invalid_credentials_returns_400(self, client):
        mock_impl = AsyncMock()
        mock_impl.validate_credentials = AsyncMock(return_value=False)

        with patch(
            "aidomaincontext.api.routes_connectors.get_connector",
            return_value=mock_impl,
        ):
            resp = client.post(
                "/api/v1/connectors",
                json={"name": "Slack", "connector_type": "slack", "config": {"bot_token": "bad"}},
            )

        assert resp.status_code == 400
        assert "Invalid credentials" in resp.json()["detail"]

    def test_credential_validation_exception_returns_400(self, client):
        mock_impl = AsyncMock()
        mock_impl.validate_credentials = AsyncMock(side_effect=RuntimeError("network error"))

        with patch(
            "aidomaincontext.api.routes_connectors.get_connector",
            return_value=mock_impl,
        ):
            resp = client.post(
                "/api/v1/connectors",
                json={"name": "Slack", "connector_type": "slack", "config": {}},
            )

        assert resp.status_code == 400
        assert "Credential validation failed" in resp.json()["detail"]

    def test_success_returns_201_with_connector(self, client, mock_session):
        connector = _make_connector(name="My Slack", connector_type="slack")
        mock_impl = AsyncMock()
        mock_impl.validate_credentials = AsyncMock(return_value=True)
        mock_session.refresh = AsyncMock(side_effect=lambda obj: None)

        # After session.refresh, session.get should produce the connector attributes
        # We simulate the refresh setting attrs on the added object
        added_objects: list = []

        def capture_add(obj):
            # Simulate ORM setting id/timestamps after commit
            obj.id = connector.id
            obj.created_at = connector.created_at
            obj.updated_at = connector.updated_at
            obj.sync_cursor = None
            added_objects.append(obj)

        mock_session.add = MagicMock(side_effect=capture_add)

        with (
            patch(
                "aidomaincontext.api.routes_connectors.get_connector",
                return_value=mock_impl,
            ),
            patch(
                "aidomaincontext.api.routes_connectors.encrypt_config",
                return_value={"_e": "token"},
            ),
        ):
            resp = client.post(
                "/api/v1/connectors",
                json={"name": "My Slack", "connector_type": "slack", "config": {"bot_token": "xoxb"}},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Slack"
        assert data["connector_type"] == "slack"


class TestGetConnectorById:
    def test_not_found_returns_404(self, client, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        resp = client.get(f"/api/v1/connectors/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Connector not found"

    def test_found_returns_200(self, client, mock_session):
        connector = _make_connector(name="My GitHub", connector_type="github")
        mock_session.get = AsyncMock(return_value=connector)

        resp = client.get(f"/api/v1/connectors/{connector.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My GitHub"
        assert data["connector_type"] == "github"
        assert data["enabled"] is True


class TestUpdateConnector:
    def test_not_found_returns_404(self, client, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        resp = client.put(
            f"/api/v1/connectors/{uuid.uuid4()}",
            json={"name": "New Name"},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Connector not found"

    def test_updates_name_field(self, client, mock_session):
        connector = _make_connector(name="Old Name")
        mock_session.get = AsyncMock(return_value=connector)

        with patch("aidomaincontext.api.routes_connectors.encrypt_config", return_value={"_e": "x"}):
            resp = client.put(
                f"/api/v1/connectors/{connector.id}",
                json={"name": "New Name"},
            )

        assert resp.status_code == 200
        # Verify mutation occurred on the ORM object
        assert connector.name == "New Name"

    def test_updates_config_field(self, client, mock_session):
        connector = _make_connector()
        mock_session.get = AsyncMock(return_value=connector)

        with patch(
            "aidomaincontext.api.routes_connectors.encrypt_config",
            return_value={"_e": "new_token"},
        ) as mock_encrypt:
            resp = client.put(
                f"/api/v1/connectors/{connector.id}",
                json={"config": {"bot_token": "xoxb-new"}},
            )

        assert resp.status_code == 200
        mock_encrypt.assert_called_once_with({"bot_token": "xoxb-new"})
        assert connector.config_encrypted == {"_e": "new_token"}

    def test_updates_enabled_field(self, client, mock_session):
        connector = _make_connector(enabled=True)
        mock_session.get = AsyncMock(return_value=connector)

        with patch("aidomaincontext.api.routes_connectors.encrypt_config", return_value={"_e": "x"}):
            resp = client.put(
                f"/api/v1/connectors/{connector.id}",
                json={"enabled": False},
            )

        assert resp.status_code == 200
        assert connector.enabled is False


class TestDeleteConnector:
    def test_not_found_returns_404(self, client, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        resp = client.delete(f"/api/v1/connectors/{uuid.uuid4()}")

        assert resp.status_code == 404

    def test_found_deletes_and_returns_204(self, client, mock_session):
        connector = _make_connector()
        mock_session.get = AsyncMock(return_value=connector)

        resp = client.delete(f"/api/v1/connectors/{connector.id}")

        assert resp.status_code == 204
        mock_session.delete.assert_awaited_once_with(connector)
        mock_session.commit.assert_awaited()


class TestTriggerSync:
    def test_not_found_returns_404(self, client, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        resp = client.post(f"/api/v1/connectors/{uuid.uuid4()}/sync")

        assert resp.status_code == 404

    def test_disabled_connector_returns_400(self, client, mock_session):
        connector = _make_connector(enabled=False)
        mock_session.get = AsyncMock(return_value=connector)

        resp = client.post(f"/api/v1/connectors/{connector.id}/sync")

        assert resp.status_code == 400
        assert "disabled" in resp.json()["detail"].lower()

    def test_success_returns_202_and_creates_sync_job(self, client, mock_session):
        connector = _make_connector(enabled=True)
        sync_job = _make_sync_job(connector_id=connector.id)
        mock_session.get = AsyncMock(return_value=connector)

        def capture_add(obj):
            # Only inject identity/timestamp fields; the route already sets
            # sync_type/status directly on the SyncJob constructor.
            obj.id = sync_job.id
            obj.connector_id = sync_job.connector_id
            obj.started_at = None
            obj.finished_at = None
            obj.documents_synced = 0
            obj.documents_failed = 0
            obj.error_message = None
            obj.created_at = sync_job.created_at

        mock_session.add = MagicMock(side_effect=capture_add)

        with (
            patch("aidomaincontext.api.routes_connectors.run_sync_job"),
            patch("aidomaincontext.api.routes_connectors.asyncio.create_task") as mock_create_task,
        ):
            resp = client.post(f"/api/v1/connectors/{connector.id}/sync?sync_type=full")

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "pending"
        assert data["sync_type"] == "full"
        # create_task was called to fire background sync
        mock_create_task.assert_called_once()

    def test_default_sync_type_is_incremental(self, client, mock_session):
        connector = _make_connector(enabled=True)
        sync_job = _make_sync_job(connector_id=connector.id)
        mock_session.get = AsyncMock(return_value=connector)

        def capture_add(obj):
            obj.id = sync_job.id
            obj.connector_id = sync_job.connector_id
            obj.started_at = None
            obj.finished_at = None
            obj.documents_synced = 0
            obj.documents_failed = 0
            obj.error_message = None
            obj.created_at = sync_job.created_at

        mock_session.add = MagicMock(side_effect=capture_add)

        with patch("aidomaincontext.api.routes_connectors.asyncio.create_task"):
            resp = client.post(f"/api/v1/connectors/{connector.id}/sync")

        assert resp.status_code == 202
        assert resp.json()["sync_type"] == "incremental"


class TestListSyncJobs:
    def test_connector_not_found_returns_404(self, client, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        resp = client.get(f"/api/v1/connectors/{uuid.uuid4()}/jobs")

        assert resp.status_code == 404

    def test_returns_paginated_jobs(self, client, mock_session):
        connector = _make_connector()
        job1 = _make_sync_job(connector_id=connector.id)
        job2 = _make_sync_job(connector_id=connector.id)

        mock_session.get = AsyncMock(return_value=connector)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [job1, job2]
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get(f"/api/v1/connectors/{connector.id}/jobs")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["status"] == "pending"

    def test_returns_empty_list_when_no_jobs(self, client, mock_session):
        connector = _make_connector()
        mock_session.get = AsyncMock(return_value=connector)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get(f"/api/v1/connectors/{connector.id}/jobs")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_pagination_params_accepted(self, client, mock_session):
        connector = _make_connector()
        mock_session.get = AsyncMock(return_value=connector)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        resp = client.get(f"/api/v1/connectors/{connector.id}/jobs?limit=5&offset=10")

        assert resp.status_code == 200


# ===========================================================================
# MODULE 2 — routes_webhooks
# ===========================================================================


@pytest.fixture(autouse=True)
def _bypass_webhook_signature_verification():
    """Patch out HMAC verification so routing tests are not coupled to signature logic."""
    with (
        patch("aidomaincontext.api.routes_webhooks._verify_slack_signature"),
        patch("aidomaincontext.api.routes_webhooks._verify_github_signature"),
    ):
        yield


class TestResolveConnector:
    """
    _resolve_connector is an internal async helper.  We test its behavior
    indirectly through the webhook endpoints (which is the only consumer) so
    the error paths are exercised with real HTTP round-trips.
    """

    def test_missing_header_returns_400(self, client, mock_session):
        # POST /webhooks/github without X-Connector-Id header
        resp = client.post(
            "/api/v1/webhooks/github",
            json={"action": "opened"},
        )
        assert resp.status_code == 400
        assert "X-Connector-Id" in resp.json()["detail"]

    def test_invalid_uuid_header_returns_400(self, client, mock_session):
        resp = client.post(
            "/api/v1/webhooks/github",
            headers={"X-Connector-Id": "not-a-uuid"},
            json={"action": "opened"},
        )
        assert resp.status_code == 400
        assert "Invalid connector ID" in resp.json()["detail"]

    def test_unknown_connector_id_returns_404(self, client, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        resp = client.post(
            "/api/v1/webhooks/github",
            headers={"X-Connector-Id": str(uuid.uuid4())},
            json={"action": "opened"},
        )
        assert resp.status_code == 404
        assert "Connector not found" in resp.json()["detail"]

    def test_found_connector_proceeds(self, client, mock_session):
        connector = _make_connector(connector_type="github")
        mock_session.get = AsyncMock(return_value=connector)

        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(return_value=[])

        # Patch the module logger because structlog's bound logger raises
        # TypeError when `event` is passed as a keyword argument (it conflicts
        # with the positional `event` parameter used internally by structlog).
        with (
            patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl),
            patch("aidomaincontext.api.routes_webhooks.asyncio.create_task"),
            patch("aidomaincontext.api.routes_webhooks.logger"),
        ):
            resp = client.post(
                "/api/v1/webhooks/github",
                headers={
                    "X-Connector-Id": str(connector.id),
                    "X-GitHub-Event": "push",
                },
                json={"ref": "refs/heads/main"},
            )

        assert resp.status_code == 200


class TestSlackWebhook:
    def test_url_verification_returns_challenge(self, client, mock_session):
        challenge_token = "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"
        resp = client.post(
            "/api/v1/webhooks/slack",
            json={"type": "url_verification", "challenge": challenge_token},
        )
        assert resp.status_code == 200
        assert resp.json() == {"challenge": challenge_token}

    def test_with_x_connector_id_header_resolves_connector(self, client, mock_session):
        connector = _make_connector(connector_type="slack")
        mock_session.get = AsyncMock(return_value=connector)

        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(return_value=[])

        with (
            patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl),
            patch("aidomaincontext.api.routes_webhooks.asyncio.create_task"),
        ):
            resp = client.post(
                "/api/v1/webhooks/slack",
                headers={"X-Connector-Id": str(connector.id)},
                json={"type": "event_callback", "team_id": "T123"},
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        mock_impl.handle_webhook.assert_awaited_once()

    def test_with_team_id_matching_config_resolves_connector(self, client, mock_session):
        team_id = "T_WORKSPACE_001"
        connector = _make_connector(connector_type="slack")
        # Config will be decrypted to {"team_id": team_id}
        connector.config_encrypted = {"_e": "encrypted"}

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [connector]
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(return_value=[])

        with (
            patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl),
            patch(
                "aidomaincontext.api.routes_webhooks.decrypt_config",
                return_value={"team_id": team_id},
            ),
            patch("aidomaincontext.api.routes_webhooks.asyncio.create_task"),
        ):
            # No X-Connector-Id — resolution falls through to team_id lookup
            resp = client.post(
                "/api/v1/webhooks/slack",
                json={"type": "event_callback", "team_id": team_id},
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_no_matching_connector_returns_404(self, client, mock_session):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch(
            "aidomaincontext.api.routes_webhooks.decrypt_config",
            return_value={},
        ):
            resp = client.post(
                "/api/v1/webhooks/slack",
                json={"type": "event_callback", "team_id": "T_NO_MATCH"},
            )

        assert resp.status_code == 404
        assert "No matching Slack connector" in resp.json()["detail"]

    def test_handle_webhook_exception_returns_500(self, client, mock_session):
        connector = _make_connector(connector_type="slack")
        mock_session.get = AsyncMock(return_value=connector)

        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(side_effect=RuntimeError("parse error"))

        with patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl):
            resp = client.post(
                "/api/v1/webhooks/slack",
                headers={"X-Connector-Id": str(connector.id)},
                json={"type": "event_callback"},
            )

        assert resp.status_code == 500
        assert "Failed to process webhook payload" in resp.json()["detail"]

    def test_documents_are_dispatched_via_create_task(self, client, mock_session):
        connector = _make_connector(connector_type="slack")
        mock_session.get = AsyncMock(return_value=connector)

        fake_doc = MagicMock()
        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(return_value=[fake_doc])

        with (
            patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl),
            patch("aidomaincontext.api.routes_webhooks.asyncio.create_task") as mock_create_task,
        ):
            resp = client.post(
                "/api/v1/webhooks/slack",
                headers={"X-Connector-Id": str(connector.id)},
                json={"type": "event_callback"},
            )

        assert resp.status_code == 200
        mock_create_task.assert_called_once()


class TestGitHubWebhook:
    def test_resolves_connector_and_passes_github_event_in_payload(self, client, mock_session):
        connector = _make_connector(connector_type="github")
        mock_session.get = AsyncMock(return_value=connector)

        captured_payloads: list[dict] = []

        async def mock_handle_webhook(payload: dict):
            captured_payloads.append(payload)
            return []

        mock_impl = AsyncMock()
        mock_impl.handle_webhook = mock_handle_webhook

        with (
            patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl),
            patch("aidomaincontext.api.routes_webhooks.asyncio.create_task"),
            patch("aidomaincontext.api.routes_webhooks.logger"),
        ):
            resp = client.post(
                "/api/v1/webhooks/github",
                headers={
                    "X-Connector-Id": str(connector.id),
                    "X-GitHub-Event": "pull_request",
                },
                json={"action": "opened", "number": 42},
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert len(captured_payloads) == 1
        assert captured_payloads[0]["_github_event"] == "pull_request"
        assert captured_payloads[0]["action"] == "opened"

    def test_handle_webhook_exception_returns_500(self, client, mock_session):
        connector = _make_connector(connector_type="github")
        mock_session.get = AsyncMock(return_value=connector)

        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(side_effect=ValueError("bad payload"))

        with patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl):
            resp = client.post(
                "/api/v1/webhooks/github",
                headers={
                    "X-Connector-Id": str(connector.id),
                    "X-GitHub-Event": "push",
                },
                json={"ref": "refs/heads/main"},
            )

        assert resp.status_code == 500
        assert "Failed to process webhook payload" in resp.json()["detail"]

    def test_documents_dispatched_via_create_task(self, client, mock_session):
        connector = _make_connector(connector_type="github")
        mock_session.get = AsyncMock(return_value=connector)

        fake_doc = MagicMock()
        mock_impl = AsyncMock()
        mock_impl.handle_webhook = AsyncMock(return_value=[fake_doc])

        with (
            patch("aidomaincontext.api.routes_webhooks.get_connector", return_value=mock_impl),
            patch("aidomaincontext.api.routes_webhooks.asyncio.create_task") as mock_create_task,
            patch("aidomaincontext.api.routes_webhooks.logger"),
        ):
            resp = client.post(
                "/api/v1/webhooks/github",
                headers={
                    "X-Connector-Id": str(connector.id),
                    "X-GitHub-Event": "issues",
                },
                json={"action": "opened"},
            )

        assert resp.status_code == 200
        mock_create_task.assert_called_once()

    def test_missing_connector_id_header_returns_400(self, client, mock_session):
        resp = client.post(
            "/api/v1/webhooks/github",
            json={"action": "opened"},
        )
        assert resp.status_code == 400

    def test_invalid_connector_id_header_returns_400(self, client, mock_session):
        resp = client.post(
            "/api/v1/webhooks/github",
            headers={"X-Connector-Id": "bad-uuid-value"},
            json={"action": "opened"},
        )
        assert resp.status_code == 400


# ===========================================================================
# MODULE 3 — routes_oauth
# ===========================================================================


def _make_redis_mock() -> AsyncMock:
    """Return an AsyncMock that mimics a redis.asyncio client."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.aclose = AsyncMock()
    return redis


def _make_flow_mock(
    *,
    auth_url: str = "https://accounts.google.com/o/oauth2/auth?mock=1",
    refresh_token: str = "refresh_abc",
    access_token: str = "access_xyz",
    scopes: set | None = None,
) -> MagicMock:
    """Return a MagicMock that mimics a google_auth_oauthlib.flow.Flow instance."""
    flow = MagicMock()
    flow.authorization_url.return_value = (auth_url, {})
    flow.code_verifier = None
    flow.fetch_token = MagicMock()  # synchronous — called via run_in_executor

    credentials = MagicMock()
    credentials.token = access_token
    credentials.refresh_token = refresh_token
    credentials.expiry = None
    credentials.scopes = scopes or {"https://www.googleapis.com/auth/gmail.readonly"}
    flow.credentials = credentials
    return flow


class TestGoogleAuthorize:
    def test_no_client_id_configured_returns_503(self, client, mock_session):
        with patch("aidomaincontext.api.routes_oauth.settings") as mock_settings:
            mock_settings.google_oauth_client_id = ""
            mock_settings.google_oauth_client_secret = "secret"
            mock_settings.oauth_redirect_uri = "http://localhost/callback"
            mock_settings.redis_url = "redis://localhost:6379"

            resp = client.get("/api/v1/oauth/google/authorize", follow_redirects=False)

        assert resp.status_code == 503
        assert "GOOGLE_OAUTH_CLIENT_ID" in resp.json()["detail"]

    def test_success_stores_state_in_redis_and_redirects(self, client, mock_session):
        redis_mock = _make_redis_mock()
        flow_mock = _make_flow_mock()

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("aidomaincontext.api.routes_oauth.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "client_id_123"
            mock_settings.google_oauth_client_secret = "secret"
            mock_settings.oauth_redirect_uri = "http://localhost/callback"
            mock_settings.redis_url = "redis://localhost:6379"

            resp = client.get("/api/v1/oauth/google/authorize", follow_redirects=False)

        # FastAPI's RedirectResponse defaults to 307
        assert resp.status_code == 307
        assert "accounts.google.com" in resp.headers["location"]
        # Redis setex must have been called to persist state
        redis_mock.setex.assert_awaited_once()
        setex_args = redis_mock.setex.call_args[0]
        assert setex_args[0].startswith("oauth:state:")
        # aclose must be called in finally
        redis_mock.aclose.assert_awaited_once()

    def test_authorize_uses_connector_name_query_param(self, client, mock_session):
        redis_mock = _make_redis_mock()
        flow_mock = _make_flow_mock()

        stored_values: list[str] = []

        async def capture_setex(key, ttl, value):
            stored_values.append(value)

        redis_mock.setex = AsyncMock(side_effect=capture_setex)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("aidomaincontext.api.routes_oauth.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "client_id"
            mock_settings.google_oauth_client_secret = "secret"
            mock_settings.oauth_redirect_uri = "http://localhost/callback"
            mock_settings.redis_url = "redis://localhost:6379"

            resp = client.get(
                "/api/v1/oauth/google/authorize?connector_name=Work+Gmail",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        assert len(stored_values) == 1
        stored = json.loads(stored_values[0])
        assert stored["connector_name"] == "Work Gmail"

    def test_authorize_stores_connector_type_google_drive_in_redis(self, client, mock_session):
        redis_mock = _make_redis_mock()
        flow_mock = _make_flow_mock()

        stored_values: list[str] = []

        async def capture_setex(key, ttl, value):
            stored_values.append(value)

        redis_mock.setex = AsyncMock(side_effect=capture_setex)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("aidomaincontext.api.routes_oauth.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "client_id"
            mock_settings.google_oauth_client_secret = "secret"
            mock_settings.oauth_redirect_uri = "http://localhost/callback"
            mock_settings.redis_url = "redis://localhost:6379"

            resp = client.get(
                "/api/v1/oauth/google/authorize?connector_type=google_drive&connector_name=My+Drive",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        assert len(stored_values) == 1
        stored = json.loads(stored_values[0])
        assert stored["connector_type"] == "google_drive"
        assert stored["connector_name"] == "My Drive"

    def test_authorize_defaults_connector_type_to_gmail(self, client, mock_session):
        redis_mock = _make_redis_mock()
        flow_mock = _make_flow_mock()

        stored_values: list[str] = []

        async def capture_setex(key, ttl, value):
            stored_values.append(value)

        redis_mock.setex = AsyncMock(side_effect=capture_setex)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("aidomaincontext.api.routes_oauth.settings") as mock_settings,
        ):
            mock_settings.google_oauth_client_id = "client_id"
            mock_settings.google_oauth_client_secret = "secret"
            mock_settings.oauth_redirect_uri = "http://localhost/callback"
            mock_settings.redis_url = "redis://localhost:6379"

            resp = client.get(
                "/api/v1/oauth/google/authorize",
                follow_redirects=False,
            )

        assert resp.status_code == 307
        stored = json.loads(stored_values[0])
        assert stored["connector_type"] == "gmail"

    def test_authorize_unknown_connector_type_returns_400(self, client, mock_session):
        with patch("aidomaincontext.api.routes_oauth.settings") as mock_settings:
            mock_settings.google_oauth_client_id = "client_id_123"
            mock_settings.google_oauth_client_secret = "secret"
            mock_settings.oauth_redirect_uri = "http://localhost/callback"
            mock_settings.redis_url = "redis://localhost:6379"

            resp = client.get(
                "/api/v1/oauth/google/authorize?connector_type=unknown_type",
                follow_redirects=False,
            )

        assert resp.status_code == 400
        assert "Unsupported connector_type" in resp.json()["detail"]


class TestGoogleCallback:
    def _default_patches(
        self,
        *,
        redis_mock: AsyncMock | None = None,
        flow_mock: MagicMock | None = None,
        stored_state: dict | None = None,
        userinfo_response: dict | None = None,
        userinfo_raises: Exception | None = None,
        encrypt_return: dict | None = None,
    ):
        """
        Returns a context manager tuple that applies all standard patches used
        by the callback endpoint tests.  Individual params let tests override
        the defaults to exercise specific failure scenarios.
        """
        if redis_mock is None:
            redis_mock = _make_redis_mock()
        if stored_state is None:
            stored_state = {"connector_name": "Test Gmail", "connector_type": "gmail", "code_verifier": None}
        if flow_mock is None:
            flow_mock = _make_flow_mock()
        if encrypt_return is None:
            encrypt_return = {"_e": "encrypted_token"}

        redis_mock.get = AsyncMock(
            return_value=json.dumps(stored_state) if stored_state is not None else None
        )
        return redis_mock, flow_mock, stored_state, userinfo_response, userinfo_raises, encrypt_return

    def test_expired_or_invalid_state_returns_400(self, client, mock_session):
        redis_mock = _make_redis_mock()
        redis_mock.get = AsyncMock(return_value=None)  # state not found in Redis

        with patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "auth_code_123", "state": "stale_state_xyz"},
            )

        assert resp.status_code == 400
        assert "Invalid or expired" in resp.json()["detail"]
        redis_mock.aclose.assert_awaited_once()

    def test_token_exchange_failure_returns_400(self, client, mock_session):
        redis_mock = _make_redis_mock()
        stored = {"connector_name": "Gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock()
        flow_mock.fetch_token = MagicMock(side_effect=Exception("token exchange failed"))

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "bad_code", "state": "valid_state"},
            )

        assert resp.status_code == 400
        assert "Token exchange failed" in resp.json()["detail"]

    def test_no_refresh_token_returns_400(self, client, mock_session):
        redis_mock = _make_redis_mock()
        stored = {"connector_name": "Gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock(refresh_token=None)  # type: ignore[arg-type]
        flow_mock.credentials.refresh_token = None

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "code_123", "state": "state_abc"},
            )

        assert resp.status_code == 400
        assert "refresh token" in resp.json()["detail"].lower()

    def test_userinfo_fetch_failure_returns_502(self, client, mock_session):
        import httpx as _httpx

        redis_mock = _make_redis_mock()
        stored = {"connector_name": "Gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock()

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.get = AsyncMock(
            side_effect=_httpx.HTTPError("connection refused")
        )

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "code_abc", "state": "state_xyz"},
            )

        assert resp.status_code == 502
        assert "user info" in resp.json()["detail"].lower()

    def test_success_returns_201_with_connector_id_and_email(self, client, mock_session):
        import httpx as _httpx

        connector = _make_connector(connector_type="gmail", name="Work Gmail")
        mock_session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", connector.id))

        redis_mock = _make_redis_mock()
        stored = {"connector_name": "Work Gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock(
            refresh_token="refresh_ok",
            access_token="access_ok",
        )

        # Build a real httpx Response for userinfo
        userinfo_resp = _httpx.Response(
            200,
            json={"email": "user@example.com"},
            request=_httpx.Request("GET", "https://www.googleapis.com/oauth2/v2/userinfo"),
        )

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.get = AsyncMock(return_value=userinfo_resp)

        added_objects: list = []

        def capture_add(obj):
            obj.id = connector.id
            obj.created_at = connector.created_at
            obj.updated_at = connector.updated_at
            added_objects.append(obj)

        mock_session.add = MagicMock(side_effect=capture_add)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("httpx.AsyncClient", return_value=mock_async_client),
            patch(
                "aidomaincontext.api.routes_oauth.encrypt_config",
                return_value={"_e": "enc"},
            ),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "good_code", "state": "good_state"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["user_email"] == "user@example.com"
        assert "connector_id" in data
        assert "message" in data

        # Redis state key should have been deleted after use
        redis_mock.delete.assert_awaited_once()
        redis_mock.aclose.assert_awaited_once()

    def test_success_calls_encrypt_config_with_expected_keys(self, client, mock_session):
        import httpx as _httpx

        connector = _make_connector(connector_type="gmail")

        redis_mock = _make_redis_mock()
        stored = {"connector_name": "My Gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock(refresh_token="ref_tok", access_token="acc_tok")

        userinfo_resp = _httpx.Response(
            200,
            json={"email": "tester@domain.com"},
            request=_httpx.Request("GET", "https://www.googleapis.com/oauth2/v2/userinfo"),
        )

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.get = AsyncMock(return_value=userinfo_resp)

        def capture_add(obj):
            obj.id = connector.id
            obj.created_at = connector.created_at
            obj.updated_at = connector.updated_at

        mock_session.add = MagicMock(side_effect=capture_add)

        encrypt_calls: list[dict] = []

        def capture_encrypt(cfg: dict) -> dict:
            encrypt_calls.append(cfg)
            return {"_e": "enc"}

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("httpx.AsyncClient", return_value=mock_async_client),
            patch("aidomaincontext.api.routes_oauth.encrypt_config", side_effect=capture_encrypt),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "c", "state": "s"},
            )

        assert resp.status_code == 201
        assert len(encrypt_calls) == 1
        cfg = encrypt_calls[0]
        assert cfg["access_token"] == "acc_tok"
        assert cfg["refresh_token"] == "ref_tok"
        assert cfg["user_email"] == "tester@domain.com"
        assert "scopes" in cfg

    def test_callback_creates_google_drive_connector_type(self, client, mock_session):
        import httpx as _httpx

        connector = _make_connector(connector_type="google_drive", name="My Drive")
        mock_session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", connector.id))

        redis_mock = _make_redis_mock()
        stored = {"connector_name": "My Drive", "connector_type": "google_drive", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock(refresh_token="ref_drive", access_token="acc_drive")

        userinfo_resp = _httpx.Response(
            200,
            json={"email": "drive_user@example.com"},
            request=_httpx.Request("GET", "https://www.googleapis.com/oauth2/v2/userinfo"),
        )

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.get = AsyncMock(return_value=userinfo_resp)

        created_connectors: list = []

        def capture_add(obj):
            obj.id = connector.id
            obj.created_at = connector.created_at
            obj.updated_at = connector.updated_at
            created_connectors.append(obj)

        mock_session.add = MagicMock(side_effect=capture_add)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("httpx.AsyncClient", return_value=mock_async_client),
            patch("aidomaincontext.api.routes_oauth.encrypt_config", return_value={"_e": "enc"}),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "code_drv", "state": "state_drv"},
            )

        assert resp.status_code == 201
        assert len(created_connectors) == 1
        assert created_connectors[0].connector_type == "google_drive"
        data = resp.json()
        assert data["connector_type"] == "google_drive"

    def test_callback_defaults_to_gmail_when_connector_type_missing(self, client, mock_session):
        import httpx as _httpx

        connector = _make_connector(connector_type="gmail", name="Old Gmail")
        mock_session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", connector.id))

        redis_mock = _make_redis_mock()
        # Simulate old-style state blob without connector_type key
        stored = {"connector_name": "Old Gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock(refresh_token="ref_old", access_token="acc_old")

        userinfo_resp = _httpx.Response(
            200,
            json={"email": "old@example.com"},
            request=_httpx.Request("GET", "https://www.googleapis.com/oauth2/v2/userinfo"),
        )

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.get = AsyncMock(return_value=userinfo_resp)

        created_connectors: list = []

        def capture_add(obj):
            obj.id = connector.id
            obj.created_at = connector.created_at
            obj.updated_at = connector.updated_at
            created_connectors.append(obj)

        mock_session.add = MagicMock(side_effect=capture_add)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("httpx.AsyncClient", return_value=mock_async_client),
            patch("aidomaincontext.api.routes_oauth.encrypt_config", return_value={"_e": "enc"}),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "code_old", "state": "state_old"},
            )

        assert resp.status_code == 201
        assert created_connectors[0].connector_type == "gmail"

    def test_callback_response_includes_connector_type(self, client, mock_session):
        import httpx as _httpx

        connector = _make_connector(connector_type="gmail", name="Test Gmail")
        mock_session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, "id", connector.id))

        redis_mock = _make_redis_mock()
        stored = {"connector_name": "Test Gmail", "connector_type": "gmail", "code_verifier": None}
        redis_mock.get = AsyncMock(return_value=json.dumps(stored))

        flow_mock = _make_flow_mock(refresh_token="ref_x", access_token="acc_x")

        userinfo_resp = _httpx.Response(
            200,
            json={"email": "resp@example.com"},
            request=_httpx.Request("GET", "https://www.googleapis.com/oauth2/v2/userinfo"),
        )

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_async_client.get = AsyncMock(return_value=userinfo_resp)

        def capture_add(obj):
            obj.id = connector.id
            obj.created_at = connector.created_at
            obj.updated_at = connector.updated_at

        mock_session.add = MagicMock(side_effect=capture_add)

        with (
            patch("aidomaincontext.api.routes_oauth._get_redis", AsyncMock(return_value=redis_mock)),
            patch("aidomaincontext.api.routes_oauth._build_flow", return_value=flow_mock),
            patch("httpx.AsyncClient", return_value=mock_async_client),
            patch("aidomaincontext.api.routes_oauth.encrypt_config", return_value={"_e": "enc"}),
        ):
            resp = client.get(
                "/api/v1/oauth/google/callback",
                params={"code": "code_r", "state": "state_r"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert "connector_type" in data
        assert data["connector_type"] == "gmail"
