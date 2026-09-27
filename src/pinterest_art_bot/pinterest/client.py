"""Pinterest API client.

Layered design:

    HttpPinterestClient   real Pinterest v5 REST calls
    SafePinterestClient   hard gate in front of every write
    StubPinterestClient   deterministic in-memory double for tests

SAFETY INVARIANTS enforced here, not in calling code:
  - A write that may have reached the server but did not return a definitive
    response is AMBIGUOUS. It raises PinterestAmbiguousWriteError and is never
    retried automatically.
  - Idempotent reads (GET) are retried with bounded exponential backoff.
  - Credentials are only ever sent as a bearer header and never logged.
"""

from __future__ import annotations

import asyncio
from typing import Any, NoReturn, Protocol, runtime_checkable

import httpx
import structlog

from pinterest_art_bot.config.settings import Settings, build_settings
from pinterest_art_bot.pinterest.errors import (
    PinterestAmbiguousWriteError,
    PinterestAuthError,
    PinterestError,
    PinterestNotFoundError,
    PinterestRateLimitError,
    ProductionWriteDisabledError,
)
from pinterest_art_bot.pinterest.models import (
    CreateBoardRequest,
    CreatePinRequest,
    PinterestBoard,
    PinterestPin,
)
from pinterest_art_bot.pinterest.rate_limit import TokenBucketRateLimiter, build_rate_limiter

logger = structlog.get_logger()

INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0
DEFAULT_BOARD_PAGE_SIZE = 100
MAX_BOARD_PAGE_SIZE = 250
# Hard ceiling on board scanning, so one reconciliation cannot drain the quota.
DEFAULT_MAX_BOARD_PAGES = 10
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


@runtime_checkable
class PinterestClient(Protocol):
    """Protocol for Pinterest API interactions."""

    async def list_boards(self) -> list[PinterestBoard]:
        """List all boards for the authenticated user."""
        ...

    async def list_board_pins(
        self,
        board_id: str,
        *,
        limit: int | None = None,
        max_pages: int = 10,
    ) -> list[PinterestPin]:
        """List Pins on a board. Used for reconciliation and audit.

        ``max_pages`` bounds the scan so one call cannot drain the API budget.
        """
        ...

    async def create_board(self, request: CreateBoardRequest) -> PinterestBoard:
        """Create a new board. Raises PinterestAmbiguousWriteError on network failure."""
        ...

    async def create_pin(self, request: CreatePinRequest) -> PinterestPin:
        """Create a new Pin. Raises PinterestAmbiguousWriteError on network failure."""
        ...

    async def get_pin(self, pin_id: str) -> PinterestPin | None:
        """Get a Pin by ID. Returns None if not found."""
        ...


class HttpPinterestClient:
    """Real Pinterest v5 client.

    Writes are single-shot: any transport-level failure is surfaced as an
    ambiguous write rather than being retried, because the server may already
    have created the resource.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._settings = settings or build_settings(load_env_file=False)
        if not self._settings.is_pinterest_configured:
            raise PinterestAuthError(
                "Pinterest access token is not configured (PINTEREST_ACCESS_TOKEN is empty)."
            )
        # Headers are attached per request so an injected client is never mutated
        # and can never accidentally send requests without credentials.
        self._headers = {
            "Authorization": f"Bearer {self._settings.pinterest_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._limiter = limiter or build_rate_limiter(
            max_calls=self._settings.pinterest_calls_per_hour,
            window_seconds=3600.0,
            burst=self._settings.pinterest_calls_burst,
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._settings.pinterest_api_base_url,
            timeout=httpx.Timeout(self._settings.pinterest_request_timeout_seconds, connect=10.0),
        )

    # ── Reads (idempotent, retried) ──────────────────────────────────
    async def list_boards(self) -> list[PinterestBoard]:
        """List every board, following Pinterest's bookmark pagination."""
        boards: list[PinterestBoard] = []
        bookmark: str | None = None
        page_size = min(MAX_BOARD_PAGE_SIZE, DEFAULT_BOARD_PAGE_SIZE)

        while True:
            params: dict[str, str] = {"page_size": str(page_size)}
            if bookmark:
                params["bookmark"] = bookmark
            payload = await self._get_json("/boards", params=params)
            boards.extend(_parse_boards(payload))
            bookmark = payload.get("bookmark")
            if not bookmark:
                break

        logger.info("pinterest_boards_listed", count=len(boards))
        return boards

    async def list_board_pins(
        self,
        board_id: str,
        *,
        limit: int | None = None,
        max_pages: int = DEFAULT_MAX_BOARD_PAGES,
    ) -> list[PinterestPin]:
        """List Pins on a board, following bookmark pagination.

        ``max_pages`` bounds the scan: a board with thousands of Pins would
        otherwise spend the whole hourly API budget on a single reconciliation.
        """
        pins: list[PinterestPin] = []
        bookmark: str | None = None
        pages = 0

        while pages < max(1, max_pages):
            params: dict[str, str] = {"page_size": str(DEFAULT_BOARD_PAGE_SIZE)}
            if bookmark:
                params["bookmark"] = bookmark
            payload = await self._get_json(f"/boards/{board_id}/pins", params=params)
            pins.extend(_parse_pins(payload))
            bookmark = payload.get("bookmark")
            pages += 1
            if not bookmark:
                break
            if limit is not None and len(pins) >= limit:
                break

        return pins[:limit] if limit is not None else pins

    async def get_pin(self, pin_id: str) -> PinterestPin | None:
        """Fetch a single Pin. Returns None when it does not exist."""
        try:
            payload = await self._get_json(f"/pins/{pin_id}")
        except PinterestNotFoundError:
            return None
        parsed = _parse_pins({"items": [payload]})
        return parsed[0] if parsed else None

    # ── Writes (single-shot, never auto-retried) ────────────────────
    async def create_board(self, request: CreateBoardRequest) -> PinterestBoard:
        """Create a board.

        A transport failure here is AMBIGUOUS: the board may exist on Pinterest
        even though we never saw the response.
        """
        payload = await self._post_json("/boards", json_body=request.model_dump(exclude_none=True))
        board = _parse_boards({"items": [payload]})
        if not board:
            raise PinterestError("Pinterest returned an empty board payload.")
        created = board[0]
        logger.info("pinterest_board_created", board_id=created.id, name=created.name)
        return created

    async def create_pin(self, request: CreatePinRequest) -> PinterestPin:
        """Create a Pin.

        A transport failure here is AMBIGUOUS and must be reconciled, never retried.
        """
        body = request.model_dump(exclude_none=True)
        payload = await self._post_json("/pins", json_body=body)
        pins = _parse_pins({"items": [payload]})
        if not pins:
            raise PinterestError("Pinterest returned an empty pin payload.")
        created = pins[0]
        logger.info(
            "pinterest_pin_created",
            pin_id=created.id,
            board_id=created.board_id,
            title=created.title,
        )
        return created

    # ── Transport ───────────────────────────────────────────────────
    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET with bounded retries. Safe because reads are idempotent."""
        attempt = 0
        while True:
            attempt += 1
            await self._acquire_budget()
            try:
                response = await self._client.get(path, params=params, headers=self._headers)
            except httpx.HTTPError as e:
                if attempt > self._settings.pinterest_max_retries:
                    raise PinterestError(
                        f"Pinterest GET {path} failed after {attempt} attempts: {e}"
                    ) from e
                await self._sleep_backoff(attempt, None)
                continue

            if response.status_code == 200:
                return _as_json_object(response, path)

            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= max(
                1, self._settings.pinterest_max_retries
            ):
                if response.status_code == 429:
                    self._raise_rate_limit(response, path)
                await self._sleep_backoff(attempt, _retry_after_seconds(response))
                continue

            self._raise_for_status(response, path)

    async def _post_json(self, path: str, *, json_body: dict[str, Any]) -> dict[str, Any]:
        """POST exactly once. Transport failures become ambiguous writes."""
        await self._acquire_budget()
        try:
            response = await self._client.post(path, json=json_body, headers=self._headers)
        except httpx.HTTPError as e:
            # INVARIANT: never retry. The server may have created the resource.
            logger.error("pinterest_write_ambiguous", path=path, error=str(e))
            raise PinterestAmbiguousWriteError(
                f"Pinterest POST {path} was sent but no definitive response was received: {e}. "
                "The write must be reconciled before any retry."
            ) from e

        if response.status_code in (200, 201):
            return _as_json_object(response, path)

        if response.status_code == 429:
            # Rate limited before processing: definitively NOT created, safe to surface.
            self._raise_rate_limit(response, path)

        self._raise_for_status(response, path)

    async def _acquire_budget(self) -> None:
        """Wait for rate-limit budget before spending a call."""
        if self._limiter is not None:
            await self._limiter.acquire()

    async def _sleep_backoff(self, attempt: int, retry_after: float | None) -> None:
        """Sleep with exponential backoff, honouring Retry-After when present."""
        if retry_after is not None:
            delay = min(retry_after, MAX_BACKOFF_SECONDS)
        else:
            delay = min(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
        logger.info("pinterest_retry_backoff", attempt=attempt, delay_seconds=delay)
        await asyncio.sleep(delay)

    def _raise_rate_limit(self, response: httpx.Response, path: str) -> NoReturn:
        raise PinterestRateLimitError(
            f"Pinterest rate limit hit for {path}.",
            retry_after=_retry_after_seconds(response),
        )

    def _raise_for_status(self, response: httpx.Response, path: str) -> NoReturn:
        """Translate HTTP status codes into the Pinterest error taxonomy."""
        status = response.status_code
        detail = _safe_detail(response)
        message = f"Pinterest {path} failed with HTTP {status}: {detail}"

        if status in (401, 403):
            raise PinterestAuthError(message, status_code=status)
        if status == 404:
            raise PinterestNotFoundError(message)
        if status == 429:
            self._raise_rate_limit(response, path)
        raise PinterestError(message, status_code=status)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()


class StubPinterestClient:
    """Stub Pinterest client for development and testing.

    Never makes real API calls. Always returns fake responses.
    """

    def __init__(self) -> None:
        self._boards: list[PinterestBoard] = []
        self._pins: list[PinterestPin] = []
        self._next_board_id = 1000
        self._next_pin_id = 2000
        self._should_fail = False
        self._should_ambiguous = False

    async def list_boards(self) -> list[PinterestBoard]:
        logger.debug("stub_pinterest_list_boards", count=len(self._boards))
        return list(self._boards)

    async def list_board_pins(
        self,
        board_id: str,
        *,
        limit: int | None = None,
        max_pages: int = 10,
    ) -> list[PinterestPin]:
        pins = [pin for pin in self._pins if pin.board_id == board_id]
        return pins[:limit] if limit is not None else pins

    async def create_board(self, request: CreateBoardRequest) -> PinterestBoard:
        if self._should_ambiguous:
            raise PinterestAmbiguousWriteError(
                "Stub: simulating ambiguous write (network failure after send)."
            )
        if self._should_fail:
            raise PinterestError("Stub: simulating definitive failure.")
        board = PinterestBoard(
            id=str(self._next_board_id),
            name=request.name,
            description=request.description,
        )
        self._next_board_id += 1
        self._boards.append(board)
        logger.debug("stub_pinterest_create_board", board_id=board.id, name=board.name)
        return board

    async def create_pin(self, request: CreatePinRequest) -> PinterestPin:
        if self._should_ambiguous:
            raise PinterestAmbiguousWriteError(
                "Stub: simulating ambiguous write (network failure after send)."
            )
        if self._should_fail:
            raise PinterestError("Stub: simulating definitive failure.")
        pin = PinterestPin(
            id=str(self._next_pin_id),
            board_id=request.board_id,
            title=request.title,
            description=request.description,
            link=request.link,
            alt_text=request.alt_text,
            media_url=request.media_source_url,
        )
        self._next_pin_id += 1
        self._pins.append(pin)
        logger.debug("stub_pinterest_create_pin", pin_id=pin.id, board_id=pin.board_id)
        return pin

    async def get_pin(self, pin_id: str) -> PinterestPin | None:
        for pin in self._pins:
            if pin.id == pin_id:
                return pin
        return None

    def set_ambiguous_mode(self, enabled: bool = True) -> None:
        """Test helper: make writes raise PinterestAmbiguousWriteError."""
        self._should_ambiguous = enabled

    def set_failure_mode(self, enabled: bool = True) -> None:
        """Test helper: make writes raise a definitive PinterestError."""
        self._should_fail = enabled


class SafePinterestClient:
    """Safety wrapper that guards Pinterest writes against accidental execution.

    If publish_to_pinterest is False in settings (default), create_pin raises
    ProductionWriteDisabledError immediately before any network call.
    If pinterest_auto_create_boards is False (default), create_board raises
    ProductionWriteDisabledError.
    """

    def __init__(
        self,
        inner: PinterestClient,
        settings: Settings | None = None,
    ) -> None:
        self._inner = inner
        self._settings = settings or build_settings(load_env_file=False)

    @property
    def inner(self) -> PinterestClient:
        return self._inner

    async def list_boards(self) -> list[PinterestBoard]:
        return await self._inner.list_boards()

    async def list_board_pins(
        self,
        board_id: str,
        *,
        limit: int | None = None,
        max_pages: int = 10,
    ) -> list[PinterestPin]:
        return await self._inner.list_board_pins(board_id, limit=limit, max_pages=max_pages)

    async def create_board(self, request: CreateBoardRequest) -> PinterestBoard:
        if not self._settings.can_create_boards:
            raise ProductionWriteDisabledError(
                "Pinterest board creation is disabled "
                "(PINTEREST_AUTO_CREATE_BOARDS=false or publishing disabled)."
            )
        return await self._inner.create_board(request)

    async def create_pin(self, request: CreatePinRequest) -> PinterestPin:
        if not self._settings.can_publish:
            raise ProductionWriteDisabledError(
                "Pinterest publishing is disabled (PUBLISH_TO_PINTEREST=false "
                "or access token missing)."
            )
        return await self._inner.create_pin(request)

    async def get_pin(self, pin_id: str) -> PinterestPin | None:
        return await self._inner.get_pin(pin_id)


# ── Response parsing helpers ─────────────────────────────────────────


def _as_json_object(response: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a JSON object body, raising a typed error when malformed."""
    try:
        payload = response.json()
    except ValueError as e:
        raise PinterestError(f"Pinterest {path} returned a non-JSON body.") from e
    if not isinstance(payload, dict):
        raise PinterestError(f"Pinterest {path} returned an unexpected JSON shape.")
    return payload


def _parse_boards(payload: dict[str, Any]) -> list[PinterestBoard]:
    """Extract board objects from a Pinterest list response."""
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    boards: list[PinterestBoard] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        board_id = item.get("id")
        name = item.get("name")
        if not board_id or not name:
            continue
        boards.append(
            PinterestBoard(
                id=str(board_id),
                name=str(name),
                description=item.get("description"),
                privacy=str(item.get("privacy", "PUBLIC")),
                pin_count=item.get("pin_count"),
            )
        )
    return boards


def _parse_pins(payload: dict[str, Any]) -> list[PinterestPin]:
    """Extract pin objects from a Pinterest list or single-item response."""
    items = payload.get("items")
    if items is None:
        items = [payload] if payload.get("id") else []
    if not isinstance(items, list):
        return []
    pins: list[PinterestPin] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pin_id = item.get("id")
        if not pin_id:
            continue
        board = item.get("board")
        board_id = item.get("board_id") or (board.get("id") if isinstance(board, dict) else None)
        pins.append(
            PinterestPin(
                id=str(pin_id),
                board_id=str(board_id) if board_id else "",
                title=item.get("title"),
                description=item.get("description") or item.get("note"),
                link=item.get("link"),
                media_url=item.get("media_url"),
                alt_text=item.get("alt_text"),
                created_at=item.get("created_at"),
                url=item.get("url"),
            )
        )
    return pins


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a Retry-After header expressed in seconds."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _safe_detail(response: httpx.Response) -> str:
    """Extract a short, non-sensitive error detail from a response body."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if isinstance(message, str):
            return message[:200]
    return str(payload)[:200]
