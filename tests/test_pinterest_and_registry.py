"""Tests for Pinterest client stub and museum provider protocol."""

import pytest

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.museums.registry import MuseumRegistry
from pinterest_art_bot.pinterest.client import (
    PinterestClient,
    SafePinterestClient,
    StubPinterestClient,
)
from pinterest_art_bot.pinterest.errors import (
    PinterestAmbiguousWriteError,
    ProductionWriteDisabledError,
)
from pinterest_art_bot.pinterest.models import CreateBoardRequest, CreatePinRequest


class TestStubPinterestClient:
    """Stub client must simulate the real API surface."""

    @pytest.mark.asyncio
    async def test_list_boards_empty(self):
        client = StubPinterestClient()
        boards = await client.list_boards()
        assert boards == []

    @pytest.mark.asyncio
    async def test_create_board(self):
        client = StubPinterestClient()
        board = await client.create_board(
            CreateBoardRequest(name="Test Board", description="A test board")
        )
        assert board.id is not None
        assert board.name == "Test Board"

    @pytest.mark.asyncio
    async def test_create_pin(self):
        client = StubPinterestClient()
        pin = await client.create_pin(
            CreatePinRequest(
                board_id="1000",
                title="Test Pin",
                description="A test pin",
            )
        )
        assert pin.id is not None
        assert pin.board_id == "1000"

    @pytest.mark.asyncio
    async def test_get_pin(self):
        client = StubPinterestClient()
        pin = await client.create_pin(
            CreatePinRequest(
                board_id="1000",
                title="Test Pin",
                description="Desc",
            )
        )
        retrieved = await client.get_pin(pin.id)
        assert retrieved is not None
        assert retrieved.id == pin.id

    @pytest.mark.asyncio
    async def test_get_pin_not_found(self):
        client = StubPinterestClient()
        result = await client.get_pin("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_ambiguous_write_simulation(self):
        """Stub must be able to simulate ambiguous writes for testing."""
        client = StubPinterestClient()
        client.set_ambiguous_mode(True)
        with pytest.raises(PinterestAmbiguousWriteError):
            await client.create_pin(
                CreatePinRequest(
                    board_id="1000",
                    title="Ambiguous",
                    description="Desc",
                )
            )

    def test_stub_satisfies_protocol(self):
        client = StubPinterestClient()
        assert isinstance(client, PinterestClient)


class TestSafePinterestClient:
    """SafePinterestClient must strictly enforce safety settings."""

    @pytest.mark.asyncio
    async def test_default_settings_blocks_pin_write(self):
        """Default settings (PUBLISH_TO_PINTEREST=false) must raise ProductionWriteDisabledError."""
        inner = StubPinterestClient()
        safe_client = SafePinterestClient(inner=inner, settings=Settings(_env_file=None))  # type: ignore[call-arg]

        with pytest.raises(ProductionWriteDisabledError, match="Pinterest publishing is disabled"):
            await safe_client.create_pin(
                CreatePinRequest(
                    board_id="123",
                    title="Test",
                    description="Desc",
                )
            )

    @pytest.mark.asyncio
    async def test_default_settings_blocks_board_write(self):
        """Default settings (PINTEREST_AUTO_CREATE_BOARDS=false) must raise
        ProductionWriteDisabledError."""
        inner = StubPinterestClient()
        safe_client = SafePinterestClient(inner=inner, settings=Settings(_env_file=None))  # type: ignore[call-arg]

        with pytest.raises(ProductionWriteDisabledError, match="board creation is disabled"):
            await safe_client.create_board(CreateBoardRequest(name="New Board"))

    @pytest.mark.asyncio
    async def test_read_operations_allowed_with_default_settings(self):
        """Listing boards or getting pins is safe and allowed."""
        inner = StubPinterestClient()
        safe_client = SafePinterestClient(inner=inner, settings=Settings(_env_file=None))  # type: ignore[call-arg]
        boards = await safe_client.list_boards()
        assert boards == []


class TestMuseumRegistry:
    """Museum provider registry."""

    def test_register_and_get(self):
        # Use a simple mock that satisfies the protocol
        class FakeProvider:
            @property
            def source_id(self) -> str:
                return "fake"

            @property
            def museum_name(self) -> str:
                return "Fake Museum"

            async def discover(self, **kwargs):
                pass

            async def get_artwork(self, object_id):
                pass

            async def search(self, query):
                pass

        registry = MuseumRegistry()
        provider = FakeProvider()
        registry.register(provider)  # type: ignore[arg-type]
        assert registry.get("fake") is provider
        assert "fake" in registry
        assert len(registry) == 1

    def test_duplicate_registration_raises(self):
        class FakeProvider:
            @property
            def source_id(self) -> str:
                return "fake"

            @property
            def museum_name(self) -> str:
                return "Fake"

            async def discover(self, **kwargs):
                pass

            async def get_artwork(self, object_id):
                pass

            async def search(self, query):
                pass

        registry = MuseumRegistry()
        registry.register(FakeProvider())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="already registered"):
            registry.register(FakeProvider())  # type: ignore[arg-type]

    def test_get_nonexistent(self):
        registry = MuseumRegistry()
        assert registry.get("nonexistent") is None
