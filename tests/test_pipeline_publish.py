"""Tests for the publishing path of the pipeline.

Covers the safety-critical behaviour: budgets, reservations, board resolution,
record lifecycle, ambiguous writes, and the fact that dry-run never writes.
"""

import asyncio
import hashlib
import io

import httpx
import pytest
from PIL import Image, ImageDraw

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.domain.publication import PublicationState
from pinterest_art_bot.pinterest.client import SafePinterestClient, StubPinterestClient
from pinterest_art_bot.pinterest.errors import (
    PinterestAmbiguousWriteError,
    PinterestError,
    ProductionWriteDisabledError,
)
from pinterest_art_bot.pipeline.models import DiscoveryMode, DiscoveryRequest, PipelineDecision
from pinterest_art_bot.pipeline.runner import PipelineRunner
from pinterest_art_bot.state.repository import InMemoryStateRepository
from tests.conftest import FakeMuseumProvider


def publish_settings(**overrides) -> Settings:
    """Settings with publishing enabled and a token."""
    base = {
        "_env_file": None,
        "publish_to_pinterest": True,
        "pinterest_access_token": "test-token",
        "pinterest_max_pins_per_run": 10,
        "pinterest_max_pins_per_day": 100,
        "pipeline_max_concurrency": 1,
        "state_db_path": ":memory:",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def monet(**overrides) -> Artwork:
    """Public-domain, HIGH-confidence Impressionist artwork with an image."""
    data = {
        "identity": ArtworkIdentity(source="artic", source_object_id="101"),
        "title": "Water Lilies at Sunset",
        "artist_name": "Claude Monet",
        "style_raw": "Impressionism",
        "public_domain": True,
        "open_access": True,
        "museum_name": "Art Institute of Chicago",
        "museum_object_url": "https://www.artic.edu/artworks/101",
        "primary_image": ImageInfo(url="https://example.com/monet.jpg", width=800, height=800),
    }
    data.update(overrides)
    return Artwork(**data)  # type: ignore[arg-type]


def patterned_image(salt: str) -> Image.Image:
    """A structured image, unique per salt value.

    Flat colour images hash to degenerate perceptual hashes, so every test image
    carries a deterministic pattern.
    """
    digest = hashlib.sha256(salt.encode()).digest()
    img = Image.new("RGB", (800, 800), color=tuple(digest[:3]))
    draw = ImageDraw.Draw(img)
    step = 20 + digest[3] % 40
    for i in range(-800, 1600, step):
        draw.line([(i, 0), (i + 800, 800)], fill=(255 - digest[5], 0, 0), width=9)
    return img


def unique_jpeg(salt: str) -> bytes:
    """A valid, structured JPEG whose bytes are unique per salt value."""
    return _encode(patterned_image(salt), quality=95)


def _encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def image_client(images: dict[str, bytes] | None = None) -> httpx.AsyncClient:
    """Mock transport returning a valid, URL-unique image for every request.

    Unique bytes per URL keep exact-hash deduplication from collapsing artworks
    that a test intentionally wants processed independently. Pass ``images`` to
    pin specific bytes to specific URLs.
    """
    table = images or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg"},
            content=table.get(url, unique_jpeg(url)),
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def build_runner(
    settings: Settings,
    artworks: list[Artwork],
    client: StubPinterestClient,
    repo: InMemoryStateRepository,
) -> PipelineRunner:
    """Assemble a runner wired to a stubbed Pinterest client."""
    return PipelineRunner(
        providers={"artic": FakeMuseumProvider(artworks)},
        state_repo=repo,
        pinterest_client=SafePinterestClient(client, settings),
        settings=settings,
    )


class TestPublishDisabled:
    async def test_publish_request_without_switch_is_blocked(self):
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)

        with pytest.raises(ProductionWriteDisabledError):
            await runner.run(
                DiscoveryRequest(museum="artic", limit=1, publish=True),
                http_client=image_client(),
            )

    async def test_safe_client_blocks_writes_with_default_settings(self):
        from pinterest_art_bot.pinterest.models import CreateBoardRequest, CreatePinRequest

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        safe = SafePinterestClient(StubPinterestClient(), settings)

        with pytest.raises(ProductionWriteDisabledError):
            await safe.create_pin(CreatePinRequest(board_id="1", title="t", description="d"))

        with pytest.raises(ProductionWriteDisabledError):
            await safe.create_board(CreateBoardRequest(name="n"))

    async def test_dry_run_never_writes(self):
        settings = publish_settings(publish_to_pinterest=False, pinterest_access_token="tok")
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )

        assert summary.dry_run is True
        assert summary.pinterest_writes == 0
        assert summary.published == 0
        assert summary.ready_dry_run_plans == 1
        # Publication history must remain untouched by a dry run.
        assert await repo.artwork_exists("artic:101") is False


class TestPublishSuccess:
    async def test_publishes_and_records_state(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        # Pre-create the remote board so the ID resolves by name.
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )

        assert summary.published == 1
        assert summary.pinterest_writes == 1
        item = summary.items[0]
        assert item.decision == PipelineDecision.PUBLISHED
        assert item.pin_id is not None

        record = await repo.get_record("artic:101")
        assert record is not None
        assert record.state == PublicationState.PUBLISHED
        assert record.pin_id == item.pin_id
        assert record.board_target is not None
        assert record.board_target.board_id == board.id
        assert record.image_sha256
        assert record.link == "https://www.artic.edu/artworks/101"

    async def test_second_run_is_deduplicated(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)
        request = DiscoveryRequest(museum="artic", limit=1, publish=True)

        first = await runner.run(request, http_client=image_client())
        second = await runner.run(request, http_client=image_client())

        assert first.published == 1
        assert second.published == 0
        assert second.pinterest_writes == 0
        assert second.items[0].decision == PipelineDecision.ALREADY_PUBLISHED

    async def test_publish_requires_client(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        runner = PipelineRunner(
            providers={"artic": FakeMuseumProvider([monet()])},
            state_repo=repo,
            settings=settings,
        )
        with pytest.raises(ValueError, match="requires a Pinterest client"):
            await runner.run(
                DiscoveryRequest(museum="artic", limit=1, publish=True),
                http_client=image_client(),
            )

    async def test_missing_board_id_holds_publication(self):
        settings = publish_settings()
        client = StubPinterestClient()  # no boards exist remotely
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )

        assert summary.published == 0
        assert summary.board_id_missing == 1
        assert summary.items[0].decision == PipelineDecision.BOARD_ID_MISSING


class TestAmbiguousWrites:
    async def test_ambiguous_write_is_never_retried(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        client.set_ambiguous_mode(True)
        runner = build_runner(settings, [monet()], client, repo)
        request = DiscoveryRequest(museum="artic", limit=1, publish=True)

        first = await runner.run(request, http_client=image_client())

        assert first.publish_ambiguous == 1
        assert first.pinterest_writes == 0
        record = await repo.get_record("artic:101")
        assert record is not None
        assert record.state == PublicationState.AMBIGUOUS

        # A second run must refuse to touch it, even though the client recovered.
        client.set_ambiguous_mode(False)
        second = await runner.run(request, http_client=image_client())
        assert second.published == 0
        assert second.items[0].decision == PipelineDecision.PUBLISH_AMBIGUOUS
        assert await client.get_pin("2000") is None

    async def test_ambiguous_record_blocks_reservation(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        client.set_ambiguous_mode(True)
        runner = build_runner(settings, [monet()], client, repo)
        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )

        assert await repo.reserve_artwork("artic:101", "other_run") is False


class TestPublishFailure:
    async def test_definitive_failure_records_retry_count(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        client.set_failure_mode(True)
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )

        assert summary.publish_failed == 1
        assert summary.pinterest_writes == 0
        record = await repo.get_record("artic:101")
        assert record is not None
        assert record.state == PublicationState.FAILED
        assert record.retry_count == 1
        # A definitive failure is retryable, so the reservation is released.
        assert await repo.reserve_artwork("artic:101", "next_run") is True

    async def test_failure_surfaces_pinterest_error_text(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        client.set_failure_mode(True)
        runner = build_runner(settings, [monet()], client, repo)
        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert "failed definitively" in summary.items[0].reason


class TestBudgets:
    async def test_run_budget_is_enforced(self):
        artworks = [
            monet(
                identity=ArtworkIdentity(source="artic", source_object_id=str(200 + i)),
                title=f"Water Lilies {i}",
                primary_image=ImageInfo(
                    url=f"https://example.com/monet-{i}.jpg", width=800, height=800
                ),
            )
            for i in range(3)
        ]
        settings = publish_settings(pinterest_max_pins_per_run=1)
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, artworks, client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=3, publish=True),
            http_client=image_client(),
        )

        assert summary.published == 1
        assert summary.pinterest_writes == 1
        assert summary.count_decision(PipelineDecision.PIN_BUDGET_EXHAUSTED) == 2

    async def test_daily_budget_counts_previous_publications(self):
        from pinterest_art_bot.domain.publication import PublicationRecord

        repo = InMemoryStateRepository()
        previous = PublicationRecord(artwork_canonical_id="met:1")
        previous.transition_to(PublicationState.PUBLISHED)
        await repo.save_record(previous)

        settings = publish_settings(pinterest_max_pins_per_day=1)
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert summary.published == 0
        assert summary.items[0].decision == PipelineDecision.PIN_BUDGET_EXHAUSTED

    async def test_zero_budget_blocks_all_writes(self):
        settings = publish_settings(pinterest_max_pins_per_run=0)
        runner = build_runner(settings, [monet()], StubPinterestClient(), InMemoryStateRepository())
        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert summary.pinterest_writes == 0


class TestNearDuplicateDetection:
    async def test_near_duplicate_is_detected_by_perceptual_hash(self):
        """Re-encoded copies of the same image must not publish twice."""
        picture = patterned_image("water-lilies")
        original_bytes = _encode(picture, quality=95)
        reencoded_bytes = _encode(picture, quality=25)  # same picture, different bytes

        assert original_bytes != reencoded_bytes

        original = monet()
        reencoded = monet(
            identity=ArtworkIdentity(source="artic", source_object_id="999"),
            title="Water Lilies (re-encoded)",
            primary_image=ImageInfo(url="https://example.com/reencoded.jpg", width=800, height=800),
        )

        images = {
            original.best_image_url: original_bytes,
            reencoded.best_image_url: reencoded_bytes,
        }

        settings = publish_settings(perceptual_hash_max_distance=8)
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [original], client, repo)

        first = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(images),
        )
        assert first.published == 1

        second_runner = build_runner(settings, [reencoded], client, repo)
        second = await second_runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(images),
        )

        assert second.published == 0
        assert second.items[0].decision == PipelineDecision.DUPLICATE_NEAR_MATCH
        assert second.duplicates_near_match == 1

    async def test_distinct_images_are_not_treated_as_duplicates(self):
        settings = publish_settings(perceptual_hash_max_distance=8)
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)
        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )

        other = monet(
            identity=ArtworkIdentity(source="artic", source_object_id="777"),
            title="Haystacks",
            primary_image=ImageInfo(url="https://example.com/haystacks.jpg", width=800, height=800),
        )
        runner2 = build_runner(settings, [other], client, repo)
        second = await runner2.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert second.published == 1


class TestDiscoveryModes:
    async def test_object_id_mode(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        provider = FakeMuseumProvider([monet()])
        runner = PipelineRunner(
            providers={"artic": provider},
            state_repo=repo,
            settings=settings,
        )
        summary = await runner.run(
            DiscoveryRequest(
                museum="artic",
                limit=5,
                mode=DiscoveryMode.OBJECT_ID,
                object_ids=["101", "missing"],
                publish=False,
            ),
            http_client=image_client(),
        )
        assert summary.fetched == 1

    async def test_duplicate_canonical_ids_are_processed_once(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        runner = PipelineRunner(
            providers={"artic": FakeMuseumProvider([monet(), monet()])},
            state_repo=repo,
            settings=settings,
        )
        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=5, publish=False),
            http_client=image_client(),
        )
        assert summary.fetched == 1


def test_pinterest_error_is_base_class():
    assert issubclass(PinterestAmbiguousWriteError, PinterestError)


class TestDiscoveryCursor:
    """Successive runs must explore new ground instead of rescanning the head."""

    async def test_cursor_advances_between_runs(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        provider = FakeMuseumProvider(
            [
                monet(identity=ArtworkIdentity(source="artic", source_object_id=str(i)))
                for i in range(6)
            ]
        )
        runner = PipelineRunner(providers={"artic": provider}, state_repo=repo, settings=settings)

        first = await runner.run(
            DiscoveryRequest(museum="artic", limit=2, page_size=2, max_pages=1, publish=False),
            http_client=image_client(),
        )
        cursor = await repo.get_cursor("discovery:artic")
        assert cursor is not None and cursor > 0
        assert provider.discover_calls[0]["offset"] == 0

        second = await runner.run(
            DiscoveryRequest(museum="artic", limit=2, page_size=2, max_pages=1, publish=False),
            http_client=image_client(),
        )
        # The second run resumes after the first.
        assert provider.discover_calls[1]["offset"] == cursor
        assert await repo.get_cursor("discovery:artic") != cursor
        assert {item.canonical_id for item in first.items} != {
            item.canonical_id for item in second.items
        }

    async def test_explicit_offset_wins_over_cursor(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        await repo.set_cursor("discovery:artic", 500)
        provider = FakeMuseumProvider([monet()])
        runner = PipelineRunner(providers={"artic": provider}, state_repo=repo, settings=settings)

        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, offset=42, publish=False),
            http_client=image_client(),
        )
        assert provider.discover_calls[0]["offset"] == 42

    async def test_rotation_can_be_disabled(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        await repo.set_cursor("discovery:artic", 77)
        provider = FakeMuseumProvider([monet()])
        runner = PipelineRunner(providers={"artic": provider}, state_repo=repo, settings=settings)

        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, rotate=False, publish=False),
            http_client=image_client(),
        )
        assert provider.discover_calls[0]["offset"] == 0

    async def test_exhausted_listing_wraps_cursor_to_zero(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        await repo.set_cursor("discovery:artic", 10)
        provider = FakeMuseumProvider([monet()])
        runner = PipelineRunner(providers={"artic": provider}, state_repo=repo, settings=settings)

        await runner.run(
            DiscoveryRequest(museum="artic", limit=5, page_size=5, max_pages=1, publish=False),
            http_client=image_client(),
        )
        # Wrapping back to the start beats resuming at a stale deep offset.
        assert await repo.get_cursor("discovery:artic") == 0

    async def test_object_id_mode_ignores_cursor(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        await repo.set_cursor("discovery:artic", 99)
        provider = FakeMuseumProvider([monet()])
        runner = PipelineRunner(providers={"artic": provider}, state_repo=repo, settings=settings)

        summary = await runner.run(
            DiscoveryRequest(
                museum="artic",
                limit=1,
                mode=DiscoveryMode.OBJECT_ID,
                object_ids=["101"],
                publish=False,
            ),
            http_client=image_client(),
        )
        assert summary.fetched == 1
        assert provider.discover_calls == []


class TestRetryableStates:
    """FAILED and RECONCILED records may be attempted again; AMBIGUOUS may not."""

    async def _failed_record(self, repo, artwork_id: str = "artic:101"):
        from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState

        record = PublicationRecord(artwork_canonical_id=artwork_id)
        record.transition_to(PublicationState.RESERVED, reason="r")
        record.transition_to(PublicationState.PUBLISHING, reason="p")
        record.transition_to(PublicationState.FAILED, reason="definitive failure")
        record.retry_count = 1
        await repo.save_record(record)
        return record

    async def test_failed_artwork_is_retried(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        await self._failed_record(repo)
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )

        assert summary.retries_considered == 1
        assert summary.published == 1
        record = await repo.get_record("artic:101")
        assert record is not None
        assert record.state == PublicationState.PUBLISHED

    async def test_reconciled_artwork_is_retried(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState

        record = PublicationRecord(artwork_canonical_id="artic:101")
        record.transition_to(PublicationState.RESERVED, reason="r")
        record.transition_to(PublicationState.PUBLISHING, reason="p")
        record.transition_to(PublicationState.AMBIGUOUS, reason="timeout")
        record.transition_to(PublicationState.RECONCILED, reason="no pin found")
        await repo.save_record(record)

        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        assert summary.retries_considered == 1
        assert summary.published == 1

    async def test_ambiguous_artwork_is_still_blocked(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)
        client.set_ambiguous_mode(True)
        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )

        client.set_ambiguous_mode(False)
        second = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        assert second.retries_considered == 0
        assert second.items[0].decision == PipelineDecision.PUBLISH_AMBIGUOUS
        assert second.published == 0

    async def test_in_flight_record_is_still_skipped(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState

        record = PublicationRecord(artwork_canonical_id="artic:101")
        record.transition_to(PublicationState.RESERVED, reason="held by another run")
        await repo.save_record(record)

        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)
        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False, rotate=False),
            http_client=image_client(),
        )
        assert summary.items[0].decision == PipelineDecision.ALREADY_SEEN
        assert summary.duplicates_seen == 1
        assert summary.retries_considered == 0


class TestCrossSourceDeduplication:
    """The same work in two museums' catalogues must publish only once."""

    def other_museum_version(self) -> Artwork:
        """The same painting as `monet()`, as another museum catalogues it.

        Different title formatting, decorated artist string, different year, and
        a different photograph — so only cross-source identity can catch it.
        """
        return monet(
            identity=ArtworkIdentity(source="cleveland", source_object_id="999"),
            title="The Dance Class",
            artist_name="Edgar Degas (French, 1834-1917)",
            museum_name="Cleveland Museum of Art",
            museum_object_url="https://www.clevelandart.org/art/999",
            creation_date_text="1874",
            year_start=1874,
            primary_image=ImageInfo(
                url="https://example.com/other-museum.jpg", width=800, height=800
            ),
        )

    @staticmethod
    def first_museum_version() -> Artwork:
        return monet(
            title="Dance Class",
            artist_name="Edgar Degas",
            creation_date_text="1872",
            year_start=1872,
        )

    async def test_duplicate_work_in_another_museum_is_skipped(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))

        # First museum publishes.
        first_runner = build_runner(settings, [self.first_museum_version()], client, repo)
        first = await first_runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        assert first.published == 1

        # Second museum holds the same work; the image bytes differ, so only the
        # cross-source identity check can catch it.
        second_runner = PipelineRunner(
            providers={"cleveland": FakeMuseumProvider([self.other_museum_version()])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
        )
        second = await second_runner.run(
            DiscoveryRequest(museum="cleveland", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )

        assert second.published == 0
        assert second.duplicates_cross_source == 1
        assert second.items[0].decision == PipelineDecision.DUPLICATE_WORK
        assert "artic:101" in second.items[0].reason  # the first museum's record

    async def test_a_copy_after_another_artist_is_not_merged(self):
        """A copy is a different object and must not be hidden by a merge."""
        settings = publish_settings()
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        await build_runner(settings, [self.first_museum_version()], client, repo).run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )

        copy = monet(
            identity=ArtworkIdentity(source="cleveland", source_object_id="654"),
            title="Water Lilies after Monet",
            artist_name="Marcel Mouly",
            museum_name="Cleveland Museum of Art",
            primary_image=ImageInfo(url="https://example.com/copy.jpg", width=800, height=800),
        )
        runner = PipelineRunner(
            providers={"cleveland": FakeMuseumProvider([copy])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
        )
        summary = await runner.run(
            DiscoveryRequest(museum="cleveland", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        assert summary.published == 1
        assert summary.duplicates_cross_source == 0

    async def test_distinct_works_are_not_merged(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        await build_runner(settings, [self.first_museum_version()], client, repo).run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )

        distinct = monet(
            identity=ArtworkIdentity(source="cleveland", source_object_id="321"),
            title="Haystacks, Snow Effect",
            artist_name="Claude Monet",
            museum_name="Cleveland Museum of Art",
            primary_image=ImageInfo(url="https://example.com/haystacks.jpg", width=800, height=800),
        )
        runner = PipelineRunner(
            providers={"cleveland": FakeMuseumProvider([distinct])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
        )
        summary = await runner.run(
            DiscoveryRequest(museum="cleveland", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        assert summary.published == 1
        assert summary.duplicates_cross_source == 0

    async def test_can_be_disabled(self):
        settings = publish_settings(cross_source_dedup_enabled=False)
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        await build_runner(settings, [self.first_museum_version()], client, repo).run(
            DiscoveryRequest(museum="artic", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        runner = PipelineRunner(
            providers={"cleveland": FakeMuseumProvider([self.other_museum_version()])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
        )
        summary = await runner.run(
            DiscoveryRequest(museum="cleveland", limit=1, publish=True, rotate=False),
            http_client=image_client(),
        )
        assert summary.published == 1
        assert summary.duplicates_cross_source == 0


class TestRunHistoryIsRecorded:
    async def test_run_is_persisted(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)

        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )

        runs = await repo.list_runs()
        assert len(runs) == 1
        assert runs[0]["museum"] == "artic"
        assert runs[0]["dry_run"] is True
        assert runs[0]["fetched"] == 1
        assert runs[0]["errors"] == 0
        assert runs[0]["mode"] == "highlights"
        assert runs[0]["finished_at"]

    async def test_failed_discovery_is_recorded(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [], StubPinterestClient(), repo)

        async def boom(*args, **kwargs):
            raise RuntimeError("museum unreachable")

        runner._fetch_candidates = boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError):
            await runner.run(
                DiscoveryRequest(museum="artic", limit=1, publish=False),
                http_client=image_client(),
            )

        runs = await repo.list_runs()
        assert len(runs) == 1
        assert "museum unreachable" in runs[0]["error"]

    async def test_history_failure_never_breaks_a_run(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()

        async def broken_save_run(run_id, summary):
            raise RuntimeError("disk full")

        repo.save_run = broken_save_run  # type: ignore[method-assign]
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )
        assert summary.errors == 0
        assert summary.ready_dry_run_plans == 1


class TestBoardDiversity:
    """A single movement must not dominate a run."""

    async def test_per_board_daily_limit_blocks_excess_writes(self):
        artworks = [
            monet(
                identity=ArtworkIdentity(source="artic", source_object_id=str(300 + i)),
                title=f"Water Lilies {i}",
                primary_image=ImageInfo(
                    url=f"https://example.com/board-{i}.jpg", width=800, height=800
                ),
            )
            for i in range(4)
        ]
        settings = publish_settings(max_pins_per_board_per_day=2, pinterest_max_pins_per_run=10)
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, artworks, client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=4, publish=True),
            http_client=image_client(),
        )

        assert summary.published == 2
        assert summary.board_budget_exhausted == 2
        assert summary.count_decision(PipelineDecision.BOARD_BUDGET_EXHAUSTED) == 2

    async def test_limit_counts_previous_publications_today(self):
        from pinterest_art_bot.domain.publication import (
            BoardTarget,
            PublicationRecord,
            PublicationState,
        )

        settings = publish_settings(max_pins_per_board_per_day=1)
        repo = InMemoryStateRepository()
        # A Pin already published today on the same board.
        earlier = PublicationRecord(artwork_canonical_id="met:1")
        earlier.board_target = BoardTarget(
            board_key="impressionism", board_name="Impressionism", board_id="1000"
        )
        earlier.transition_to(PublicationState.PUBLISHED)
        await repo.save_record(earlier)

        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert summary.published == 0
        assert summary.board_budget_exhausted == 1

    async def test_zero_limit_disables_the_cap(self):
        settings = publish_settings(max_pins_per_board_per_day=0)
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)
        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert summary.published == 1
        assert summary.board_budget_exhausted == 0

    async def test_round_robin_prefers_boards_used_least(self):
        """With two ready boards, the less-used one goes first."""
        settings = publish_settings(board_round_robin=True)
        from pinterest_art_bot.domain.publication import (
            BoardTarget,
            PublicationRecord,
            PublicationState,
        )

        repo = InMemoryStateRepository()
        used = PublicationRecord(artwork_canonical_id="met:9")
        used.board_target = BoardTarget(
            board_key="impressionism", board_name="Impressionism", board_id="1000"
        )
        used.transition_to(PublicationState.PUBLISHED)
        await repo.save_record(used)

        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        await client.create_board(CreateBoardRequest(name="Baroque Art"))
        runner = build_runner(settings, [monet()], client, repo)

        assert runner.publisher is not None
        await runner._load_board_activity()
        assert runner.publisher.board_rank("baroque") < runner.publisher.board_rank("impressionism")

    async def test_rotation_can_be_disabled(self):
        settings = publish_settings(board_round_robin=False)
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)
        assert runner.settings.board_round_robin is False
        # With rotation off, the candidate order is untouched.
        candidates = [monet()]
        assert runner._spread_across_boards(candidates) == candidates


class TestBase64MediaMode:
    """Uploading the prepared image instead of hotlinking the museum URL."""

    async def test_base64_mode_uploads_the_prepared_image(self):
        from pinterest_art_bot.media.preparation import ImagePreparer, MediaMode

        settings = publish_settings(
            pin_media_mode="base64", image_prepare_enabled=True, image_max_width=400
        )
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))

        from pinterest_art_bot.pipeline.runner import PipelineRunner

        runner = PipelineRunner(
            providers={"artic": FakeMuseumProvider([monet()])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
            image_preparer=ImagePreparer(max_width=400, mode=MediaMode.BASE64),
        )
        assert runner.image_preparer is not None
        assert runner.image_preparer.mode == MediaMode.BASE64

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert summary.published == 1

        pin = await client.get_pin(summary.items[0].pin_id or "")
        assert pin is not None
        assert pin.media_url is None, "the museum URL must be replaced by an upload"
        assert pin.description

    async def test_url_mode_keeps_the_museum_url(self):
        settings = publish_settings()
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert summary.published == 1
        pin = await client.get_pin(summary.items[0].pin_id or "")
        assert pin is not None
        assert pin.media_url == "https://example.com/monet.jpg"

    async def test_recorded_fingerprints_come_from_the_original_bytes(self):
        """Re-encoding must not change what deduplication records."""
        from pinterest_art_bot.media.preparation import ImagePreparer, MediaMode
        from pinterest_art_bot.pinterest.models import CreateBoardRequest
        from pinterest_art_bot.pipeline.runner import PipelineRunner

        url = "https://example.com/monet.jpg"
        images = {url: _encode(patterned_image("fingerprint-check"), quality=95)}
        original_sha = hashlib.sha256(images[url]).hexdigest()

        settings = publish_settings(pin_media_mode="base64", image_max_width=320)
        client = StubPinterestClient()
        repo = InMemoryStateRepository()
        await client.create_board(CreateBoardRequest(name="Impressionism"))

        runner = PipelineRunner(
            providers={"artic": FakeMuseumProvider([monet()])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
            image_preparer=ImagePreparer(max_width=320, mode=MediaMode.BASE64),
        )
        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(images),
        )
        assert summary.published == 1

        record = await repo.get_record("artic:101")
        assert record is not None
        assert record.image_sha256 == original_sha, "the record must keep the original hash"
        assert record.image_perceptual_hash is not None

        # A second artwork with the same source bytes is still an exact duplicate,
        # even though every publish re-encodes the image.
        twin = monet(
            identity=ArtworkIdentity(source="cleveland", source_object_id="5151"),
            title="Water Lilies (same photograph)",
            primary_image=ImageInfo(url=url, width=800, height=800),
        )
        second = PipelineRunner(
            providers={"cleveland": FakeMuseumProvider([twin])},
            state_repo=repo,
            pinterest_client=SafePinterestClient(client, settings),
            settings=settings,
            image_preparer=ImagePreparer(max_width=320, mode=MediaMode.BASE64),
        )
        second_summary = await second.run(
            DiscoveryRequest(museum="cleveland", limit=1, publish=True),
            http_client=image_client(images),
        )
        assert second_summary.published == 0
        assert second_summary.items[0].decision == PipelineDecision.DUPLICATE_IMAGE_HASH


class TestDryRunLeavesNoTrace:
    """A dry run must not change what a later run reports."""

    async def test_dry_run_records_no_work_identity(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)

        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )

        assert await repo.list_work_identities() == [], "dry-run must not write identities"
        assert await repo.artwork_exists("artic:101") is False
        assert await repo.get_dry_run_plan("artic:101") is not None, "plans are still recorded"

    async def test_a_later_dry_run_still_reports_the_work(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)

        first = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )
        second = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False, rotate=False),
            http_client=image_client(),
        )

        assert first.count_decision(PipelineDecision.READY) == 1
        assert second.count_decision(PipelineDecision.READY) == 1
        assert second.duplicates_cross_source == 0

    async def test_publish_does_record_the_identity(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        from pinterest_art_bot.pinterest.models import CreateBoardRequest

        await client.create_board(CreateBoardRequest(name="Impressionism"))
        runner = build_runner(settings, [monet()], client, repo)

        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=True),
            http_client=image_client(),
        )
        assert len(await repo.list_work_identities()) == 1


class TestRunExclusivity:
    """One runner may only drive one run at a time."""

    async def test_concurrent_runs_are_serialised(self):
        settings = publish_settings(pipeline_max_concurrency=2)
        repo = InMemoryStateRepository()
        artwork = monet()
        provider = FakeMuseumProvider([artwork])
        runner = PipelineRunner(
            providers={"artic": provider},
            state_repo=repo,
            settings=settings,
        )

        request_a = DiscoveryRequest(museum="artic", limit=1, rotate=False)
        request_b = DiscoveryRequest(museum="artic", limit=1, rotate=False)

        # Run both concurrently; the lock must keep per-run state consistent.
        first, second = await asyncio.gather(
            runner.run(request_a, http_client=image_client()),
            runner.run(request_b, http_client=image_client()),
        )

        # Both runs must have produced internally consistent counters.
        assert first.fetched == 1
        assert second.fetched == 1
        assert first.ready_dry_run_plans + second.ready_dry_run_plans >= 1
        for summary in (first, second):
            assert summary.items, "each run reports its own items"
            assert len(summary.items) == summary.fetched


class TestHousekeeping:
    """Retention runs on every invocation, not only when publishing."""

    async def test_housekeeping_runs_in_dry_run(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()
        calls: list[str] = []

        def tracker(name: str):
            async def _track(ttl):
                calls.append(name)
                return 0

            return _track

        repo.purge_expired_reservations = tracker("reservations")
        repo.purge_dry_run_plans = tracker("dry_run_plans")
        repo.purge_run_history = tracker("runs")
        repo.purge_rejected_fingerprints = tracker("fingerprints")

        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)
        await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )

        assert calls == ["reservations", "dry_run_plans", "runs", "fingerprints"]

    async def test_housekeeping_failure_does_not_break_a_run(self):
        settings = publish_settings()
        repo = InMemoryStateRepository()

        async def broken(ttl):
            raise RuntimeError("disk error")

        repo.purge_dry_run_plans = broken
        runner = build_runner(settings, [monet()], StubPinterestClient(), repo)

        summary = await runner.run(
            DiscoveryRequest(museum="artic", limit=1, publish=False),
            http_client=image_client(),
        )
        assert summary.errors == 0
        assert summary.ready_dry_run_plans == 1
