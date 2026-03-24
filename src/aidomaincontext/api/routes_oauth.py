"""OAuth 2.0 routes for Google / Gmail and Yahoo Mail connector setup."""

from __future__ import annotations

import asyncio
import functools
import json
import secrets

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from aidomaincontext.config import settings
from aidomaincontext.models.connector import Connector
from aidomaincontext.models.database import get_session
from aidomaincontext.security import encrypt_config

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])

_SCOPES_BY_CONNECTOR: dict[str, list[str]] = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ],
    "google_drive": [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ],
}
_SUPPORTED_CONNECTOR_TYPES = frozenset(_SCOPES_BY_CONNECTOR)
_OAUTH_STATE_TTL = 600  # 10 minutes


def _build_flow(*, scopes: list[str], state: str | None = None) -> Flow:
    """Construct a google_auth_oauthlib Flow from application settings."""
    client_config = {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uris": [settings.oauth_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    kwargs: dict = {"scopes": scopes, "redirect_uri": settings.oauth_redirect_uri}
    if state is not None:
        kwargs["state"] = state
    return Flow.from_client_config(client_config, **kwargs)


async def _get_redis():
    """Return an async Redis client."""
    import redis.asyncio as aioredis  # noqa: PLC0415

    return aioredis.from_url(settings.redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/google/authorize")
async def google_authorize(
    connector_name: str = Query(default="My Gmail"),
    connector_type: str = Query(default="gmail"),
):
    """Start the Google OAuth 2.0 consent flow.

    Redirects the browser to Google's consent screen. After the user grants
    access, Google redirects back to /oauth/google/callback.
    """
    if not settings.google_oauth_client_id:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_OAUTH_CLIENT_ID is not configured on this server.",
        )

    if connector_type not in _SUPPORTED_CONNECTOR_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported connector_type '{connector_type}'.",
        )

    scopes = _SCOPES_BY_CONNECTOR[connector_type]
    state = secrets.token_urlsafe(32)

    flow = _build_flow(scopes=scopes)
    auth_url, _ = flow.authorization_url(
        state=state,
        access_type="offline",
        prompt="consent",
    )

    # Persist state + code_verifier (PKCE) so the callback can complete the exchange
    stored = json.dumps({
        "connector_name": connector_name,
        "connector_type": connector_type,
        "code_verifier": flow.code_verifier,
    })
    redis = await _get_redis()
    try:
        await redis.setex(f"oauth:state:{state}", _OAUTH_STATE_TTL, stored)
    finally:
        await redis.aclose()

    logger.info("oauth.google.authorize_redirect", connector_name=connector_name, connector_type=connector_type)
    return RedirectResponse(url=auth_url)


@router.get("/google/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Handle Google's OAuth 2.0 callback.

    Validates state (CSRF), exchanges the authorization code for tokens,
    fetches the user's email, and creates an encrypted Connector record.
    """
    # --- CSRF validation ---
    redis = await _get_redis()
    try:
        raw = await redis.get(f"oauth:state:{state}")
        if raw is None:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
        stored = json.loads(raw)
        connector_name: str = stored["connector_name"]
        connector_type: str = stored.get("connector_type", "gmail")
        code_verifier: str | None = stored.get("code_verifier")
        await redis.delete(f"oauth:state:{state}")
    finally:
        await redis.aclose()

    # --- Token exchange (synchronous library — run in thread pool) ---
    scopes = _SCOPES_BY_CONNECTOR.get(connector_type, _SCOPES_BY_CONNECTOR["gmail"])
    flow = _build_flow(scopes=scopes, state=state)
    if code_verifier:
        flow.code_verifier = code_verifier
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, functools.partial(flow.fetch_token, code=code))
    except Exception as exc:
        logger.error("oauth.google.token_exchange_failed", error=str(exc))
        raise HTTPException(status_code=400, detail="Token exchange failed.") from exc

    credentials = flow.credentials
    access_token: str = credentials.token
    refresh_token: str | None = credentials.refresh_token
    # google-auth returns a naive UTC datetime — make it timezone-aware before storing
    expiry = credentials.expiry
    if expiry is not None and expiry.tzinfo is None:
        from datetime import timezone  # noqa: PLC0415
        expiry = expiry.replace(tzinfo=timezone.utc)
    token_expiry: str | None = expiry.isoformat() if expiry else None

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google did not return a refresh token. "
                "Revoke app access in your Google account and try again."
            ),
        )

    # --- Fetch user email ---
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            user_email: str = resp.json().get("email", "unknown")
    except httpx.HTTPError as exc:
        logger.error("oauth.google.userinfo_failed", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to fetch user info from Google.") from exc

    # --- Persist connector ---
    config = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": token_expiry,
        "user_email": user_email,
        "scopes": list(credentials.scopes or scopes),
    }
    connector = Connector(
        name=connector_name,
        connector_type=connector_type,
        config_encrypted=encrypt_config(config),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)

    logger.info(
        "oauth.google.connector_created",
        connector_id=str(connector.id),
        connector_type=connector_type,
        user_email=user_email,
    )
    return JSONResponse(
        status_code=201,
        content={
            "connector_id": str(connector.id),
            "connector_type": connector_type,
            "user_email": user_email,
            "message": f"{connector_type} connector '{connector_name}' created successfully.",
        },
    )


# ---------------------------------------------------------------------------
# Yahoo OAuth 2.0
# ---------------------------------------------------------------------------

_YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
_YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
_YAHOO_USERINFO_URL = "https://api.login.yahoo.com/openid/v1/userinfo"
_YAHOO_SCOPES = "openid mail-r"


@router.get("/yahoo/authorize")
async def yahoo_authorize(
    connector_name: str = Query(default="My Yahoo Mail"),
):
    """Start the Yahoo OAuth 2.0 consent flow.

    Redirects the browser to Yahoo's consent screen. After the user grants
    access, Yahoo redirects back to /oauth/yahoo/callback.
    """
    if not settings.yahoo_oauth_client_id:
        raise HTTPException(
            status_code=503,
            detail="YAHOO_OAUTH_CLIENT_ID is not configured on this server.",
        )

    state = secrets.token_urlsafe(32)

    from urllib.parse import urlencode  # noqa: PLC0415

    params = urlencode({
        "client_id": settings.yahoo_oauth_client_id,
        "redirect_uri": settings.yahoo_oauth_redirect_uri,
        "response_type": "code",
        "scope": _YAHOO_SCOPES,
        "state": state,
    })
    auth_url = f"{_YAHOO_AUTH_URL}?{params}"

    stored = json.dumps({"connector_name": connector_name})
    redis = await _get_redis()
    try:
        await redis.setex(f"oauth:state:{state}", _OAUTH_STATE_TTL, stored)
    finally:
        await redis.aclose()

    logger.info("oauth.yahoo.authorize_redirect", connector_name=connector_name)
    return RedirectResponse(url=auth_url)


@router.get("/yahoo/callback")
async def yahoo_callback(
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """Handle Yahoo's OAuth 2.0 callback.

    Validates state (CSRF), exchanges the authorization code for tokens,
    fetches the user's email, and creates an encrypted Connector record.
    """
    # --- CSRF validation ---
    redis = await _get_redis()
    try:
        raw = await redis.get(f"oauth:state:{state}")
        if raw is None:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
        stored = json.loads(raw)
        connector_name: str = stored["connector_name"]
        await redis.delete(f"oauth:state:{state}")
    finally:
        await redis.aclose()

    # --- Token exchange ---
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _YAHOO_TOKEN_URL,
                data={
                    "client_id": settings.yahoo_oauth_client_id,
                    "client_secret": settings.yahoo_oauth_client_secret,
                    "redirect_uri": settings.yahoo_oauth_redirect_uri,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token_data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("oauth.yahoo.token_exchange_failed", error=str(exc))
        raise HTTPException(status_code=400, detail="Token exchange failed.") from exc

    from datetime import timezone  # noqa: PLC0415

    access_token: str = token_data["access_token"]
    refresh_token: str | None = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    token_expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="Yahoo did not return a refresh token. Ensure offline_access is requested.",
        )

    # --- Fetch user info ---
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _YAHOO_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            userinfo = resp.json()
    except httpx.HTTPError as exc:
        logger.error("oauth.yahoo.userinfo_failed", error=str(exc))
        raise HTTPException(status_code=502, detail="Failed to fetch user info from Yahoo.") from exc

    user_email: str = userinfo.get("email", "")
    user_id: str = userinfo.get("sub", "")

    # --- Persist connector ---
    config = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": token_expiry,
        "user_email": user_email,
        "user_id": user_id,
    }
    connector = Connector(
        name=connector_name,
        connector_type="yahoo_mail",
        config_encrypted=encrypt_config(config),
    )
    session.add(connector)
    await session.commit()
    await session.refresh(connector)

    logger.info(
        "oauth.yahoo.connector_created",
        connector_id=str(connector.id),
        user_email=user_email,
    )
    return JSONResponse(
        status_code=201,
        content={
            "connector_id": str(connector.id),
            "connector_type": "yahoo_mail",
            "user_email": user_email,
            "message": f"Yahoo Mail connector '{connector_name}' created successfully.",
        },
    )
