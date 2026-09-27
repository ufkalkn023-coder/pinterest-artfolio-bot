"""Tests for the real Pinterest v5 HTTP client.

Uses httpx MockTransport: no network access, real code paths.
"""

import httpx
import pytest

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.pinterest.client import HttpPinterestClient
from pinterest_art_bot.pinterest.errors import (
    PinterestAmbiguousWriteError,
    PinterestAuthError,
    PinterestError,
    PinterestRateLimitError,
)
from pinterest_art_bot.pinterest.models import CreateBoardRequest, CreatePinRequest

TOKEN = "test-token"


def make_settings(**overrides) -> Settings:
    """Settings with a token so the client can be constructed."""
    base = {
        "_env_file": None,
        "publish_to_pinterest": True,
        "pinterest_access_token": TOKEN,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def build_client(handler, **setting_overrides) -> HttpPinterestClient:
    """Create a client whose transport is a mock."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.pinterest.com/v5")
    return HttpPinterestClient(make_settings(**setting_overrides), client=http_client)


class TestConstruction:
    def test_requires_token(self):
        with pytest.raises(PinterestAuthError, match="access token is not configured"):
            HttpPinterestClient(Settings(_env_file=None))  # type: ignore[call-arg]

    def test_sends_bearer_authorization(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"items": []})

        client = build_client(handler)
        import asyncio

        asyncio.run(client.list_boards())
        assert captured["auth"] == f"Bearer {TOKEN}"


class TestReads:
    def test_list_boards_follows_bookmarks(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                assert "bookmark" not in request.url.params
                return httpx.Response(
                    200,
                    json={"items": [{"id": "1", "name": "Impressionism"}], "bookmark": "next"},
                )
            return httpx.Response(200, json={"items": [{"id": "2", "name": "Baroque Art"}]})

        client = build_client(handler)
        boards = asyncio_boards(client)
        assert [b.name for b in boards] == ["Impressionism", "Baroque Art"]
        assert calls["n"] == 2

    def test_list_boards_retries_on_503(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, json={"message": "unavailable"})
            return httpx.Response(200, json={"items": []})

        client = build_client(handler)
        assert asyncio_boards(client) == []
        assert calls["n"] == 3

    def test_401_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "bad token"})

        client = build_client(handler)
        with pytest.raises(PinterestAuthError):
            asyncio_boards(client)

    def test_429_raises_rate_limit_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "1"})

        client = build_client(handler, pinterest_max_retries=0)
        with pytest.raises(PinterestRateLimitError) as exc:
            asyncio_boards(client)
        assert exc.value.retry_after == 1.0

    def test_get_pin_returns_none_on_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "missing"})

        client = build_client(handler)
        assert asyncio_get_pin(client, "999") is None

    def test_get_pin_parses_single_object(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "77",
                    "board_id": "12",
                    "title": "Water Lilies",
                    "link": "https://example.org/art",
                    "url": "https://www.pinterest.com/pin/77/",
                },
            )

        client = build_client(handler)
        pin = asyncio_get_pin(client, "77")
        assert pin is not None
        assert pin.id == "77"
        assert pin.link == "https://example.org/art"
        assert pin.url == "https://www.pinterest.com/pin/77/"

    def test_list_board_pins_paginates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "bookmark" not in request.url.params:
                return httpx.Response(
                    200,
                    json={"items": [{"id": "1", "board_id": "5"}], "bookmark": "b2"},
                )
            return httpx.Response(200, json={"items": [{"id": "2", "board_id": "5"}]})

        client = build_client(handler)
        import asyncio

        pins = asyncio.run(client.list_board_pins("5"))
        assert [p.id for p in pins] == ["1", "2"]


class TestWrites:
    def test_create_pin_posts_json(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            return httpx.Response(201, json={"id": "555", "board_id": "5"})

        client = build_client(handler)
        import asyncio

        pin = asyncio.run(
            client.create_pin(
                CreatePinRequest(
                    board_id="5",
                    title="Water Lilies",
                    description="Monet",
                    link="https://example.org/art",
                    media_source_url="https://example.org/lilies.jpg",
                )
            )
        )
        assert pin.id == "555"
        assert captured["url"].endswith("/pins")
        assert '"media_source_url"' in captured["body"]

    def test_transport_failure_is_ambiguous_and_never_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("connection reset after send", request=request)

        client = build_client(handler, pinterest_max_retries=5)
        import asyncio

        with pytest.raises(PinterestAmbiguousWriteError, match="must be reconciled"):
            asyncio.run(
                client.create_pin(CreatePinRequest(board_id="5", title="T", description="D"))
            )
        assert calls["n"] == 1, "ambiguous writes must never be retried"

    def test_definitive_failure_is_not_ambiguous(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "invalid media"})

        client = build_client(handler)
        import asyncio

        with pytest.raises(PinterestError) as exc:
            asyncio.run(
                client.create_pin(CreatePinRequest(board_id="5", title="T", description="D"))
            )
        assert not isinstance(exc.value, PinterestAmbiguousWriteError)

    def test_429_on_write_is_definitive_not_ambiguous(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"message": "rate limited"})

        client = build_client(handler)
        import asyncio

        with pytest.raises(PinterestRateLimitError):
            asyncio.run(
                client.create_pin(CreatePinRequest(board_id="5", title="T", description="D"))
            )

    def test_create_board(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"id": "42", "name": "Impressionism"})

        client = build_client(handler)
        import asyncio

        board = asyncio.run(client.create_board(CreateBoardRequest(name="Impressionism")))
        assert board.id == "42"

    def test_empty_write_payload_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={})

        client = build_client(handler)
        import asyncio

        with pytest.raises(PinterestError, match="empty pin payload"):
            asyncio.run(
                client.create_pin(CreatePinRequest(board_id="5", title="T", description="D"))
            )


# ── helpers ──────────────────────────────────────────────────────────


def asyncio_boards(client: HttpPinterestClient):
    """Run list_boards on a fresh loop."""
    import asyncio

    return asyncio.run(client.list_boards())


def asyncio_get_pin(client: HttpPinterestClient, pin_id: str):
    """Run get_pin on a fresh loop."""
    import asyncio

    return asyncio.run(client.get_pin(pin_id))


class TestBoardScanLimits:
    """A board scan must stop at its budget instead of paging forever."""

    async def test_pagination_stops_at_max_pages(self):
        """Without a cap, one reconciliation could page through a huge board."""

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page", "1")
            return httpx.Response(
                200,
                json={
                    "items": [{"id": f"{page}-{i}", "board_id": "5"} for i in range(100)],
                    "bookmark": f"next-{page}",
                },
            )

        client = build_client(handler)
        pins = await client.list_board_pins("5", max_pages=2)
        assert len(pins) == 200

    async def test_scan_ends_when_bookmark_is_exhausted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "bookmark" in request.url.params:
                return httpx.Response(200, json={"items": []})
            return httpx.Response(
                200,
                json={"items": [{"id": "1", "board_id": "5"}], "bookmark": "page2"},
            )

        client = build_client(handler)
        pins = await client.list_board_pins("5", max_pages=10)
        assert [p.id for p in pins] == ["1"]

    async def test_limit_takes_precedence(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"id": str(i), "board_id": "5"} for i in range(100)],
                    "bookmark": "more",
                },
            )

        client = build_client(handler)
        pins = await client.list_board_pins("5", limit=5, max_pages=10)
        assert len(pins) == 5
