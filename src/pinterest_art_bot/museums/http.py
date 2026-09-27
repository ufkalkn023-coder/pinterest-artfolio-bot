"""Shared robust HTTP client for museum API requests.

Implements bounded retries for idempotent GET requests, exponential backoff,
Retry-After support, explicit connect/read timeouts, and structured error handling.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

DEFAULT_USER_AGENT = (
    "pinterest-artfolio-bot/0.1.0 (+https://github.com/pinterest-artfolio-bot; contact@example.com)"
)
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 5.0
RETRY_STATUS_CODES = frozenset({429, 502, 503, 504})


class MuseumHttpError(Exception):
    """Base error for museum HTTP interactions."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MuseumRateLimitError(MuseumHttpError):
    """Raised when an API rate limit is exceeded and retries are exhausted."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class MuseumTimeoutError(MuseumHttpError):
    """Raised when requests timeout after retries."""


class MuseumApiError(MuseumHttpError):
    """Raised for non-retryable 4xx/5xx responses."""


class MuseumHttpClient:
    """Robust, retry-aware HTTP client for museum GET operations."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = MAX_RETRIES,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute a safe, idempotent GET request with bounded retries and backoff.

        Only GET requests are retried. Returns parsed JSON.
        """
        response = await self.get(url, params=params, headers=headers)
        try:
            return response.json()
        except Exception as e:
            raise MuseumApiError(f"Failed to parse JSON response from {url}: {e}") from e

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute an idempotent GET request with retry policy."""
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self._max_retries:
            try:
                response = await self._client.get(url, params=params, headers=headers)

                if response.status_code == 200:
                    return response

                if response.status_code in RETRY_STATUS_CODES:
                    attempt += 1
                    if attempt > self._max_retries:
                        if response.status_code == 429:
                            raise MuseumRateLimitError(f"Rate limit exceeded for {url} (HTTP 429)")
                        raise MuseumApiError(
                            f"HTTP {response.status_code} for {url} "
                            f"after {self._max_retries} retries",
                            status_code=response.status_code,
                        )

                    retry_after = self._parse_retry_after(response)
                    backoff = (
                        retry_after
                        if retry_after is not None
                        else min(
                            INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                            MAX_BACKOFF_SECONDS,
                        )
                    )

                    logger.warning(
                        "museum_http_retry",
                        url=url,
                        status_code=response.status_code,
                        attempt=attempt,
                        backoff=backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                # 404 or other non-retryable error
                if response.status_code == 404:
                    return response

                response.raise_for_status()
                return response

            except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                attempt += 1
                last_error = e
                if attempt > self._max_retries:
                    raise MuseumTimeoutError(
                        f"Timeout connecting to {url} after {self._max_retries} attempts: {e}"
                    ) from e
                backoff = min(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "museum_http_timeout_retry", url=url, attempt=attempt, backoff=backoff
                )
                await asyncio.sleep(backoff)

            except httpx.ConnectError as e:
                attempt += 1
                last_error = e
                if attempt > self._max_retries:
                    raise MuseumApiError(
                        f"Connection failed for {url} after {self._max_retries} attempts: {e}"
                    ) from e
                backoff = min(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
                await asyncio.sleep(backoff)

        raise MuseumApiError(
            f"Failed to fetch {url} after {self._max_retries} retries: {last_error}"
        )

    def _parse_retry_after(self, response: httpx.Response) -> float | None:
        """Parse the Retry-After response header in seconds."""
        retry_header = response.headers.get("Retry-After")
        if not retry_header:
            return None
        try:
            return min(float(retry_header), MAX_BACKOFF_SECONDS)
        except (ValueError, TypeError):
            return None

    async def aclose(self) -> None:
        """Close underlying HTTP client if owned."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> MuseumHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
