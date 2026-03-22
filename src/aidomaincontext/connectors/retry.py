"""Shared exponential-backoff retry utility for HTTP connectors."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx
import structlog

logger = structlog.get_logger()

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


async def with_backoff(
    func: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retryable_status_codes: frozenset[int] = _RETRYABLE_STATUS_CODES,
) -> httpx.Response:
    """Call *func()* and retry with exponential backoff on transient failures.

    *func* must be a zero-argument async callable that returns an httpx.Response.
    On 429 responses the ``Retry-After`` header is respected when present.
    After *max_retries* failed attempts the last response is returned as-is;
    the caller should call ``raise_for_status()`` to propagate the error.
    """
    attempt = 0
    while True:
        try:
            resp = await func()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt >= max_retries:
                raise
            delay = min(base_delay * (2**attempt) + random.uniform(0, 1), max_delay)
            logger.warning(
                "connector.transient_error_retrying",
                error=str(exc),
                attempt=attempt,
                delay=round(delay, 2),
            )
            await asyncio.sleep(delay)
            attempt += 1
            continue

        if resp.status_code not in retryable_status_codes or attempt >= max_retries:
            return resp

        if resp.status_code == 429 and "Retry-After" in resp.headers:
            delay = min(float(resp.headers["Retry-After"]) + random.uniform(0, 1), max_delay)
        else:
            delay = min(base_delay * (2**attempt) + random.uniform(0, 1), max_delay)

        logger.warning(
            "connector.http_error_retrying",
            status_code=resp.status_code,
            attempt=attempt,
            delay=round(delay, 2),
        )
        await asyncio.sleep(delay)
        attempt += 1
