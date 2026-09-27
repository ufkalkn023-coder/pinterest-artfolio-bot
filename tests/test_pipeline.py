"""Tests for the end-to-end pipeline runner and dry-run execution."""

import pytest

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.domain.movement import ArtMovement, ConfidenceLevel
from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState
from pinterest_art_bot.pipeline.models import (
    DiscoveryRequest,
    PipelineDecision,
)
from pinterest_art_bot.pipeline.runner import PipelineRunner
from pinterest_art_bot.state.repository import InMemoryStateRepository
from tests.conftest import FakeMuseumProvider


class TestPipelineRunner:
    """Comprehensive pipeline orchestration tests."""

    @pytest.mark.asyncio
    async def test_end_to_end_successful_dry_run(self, test_image_bytes):
        import httpx

        # Monet Impressionist artwork with valid rights and image
        artwork = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="101"),
            title="Water Lilies at Sunset",
            artist_name="Claude Monet",
            style_raw="Impressionism",
            public_domain=True,
            open_access=True,
            museum_name="Art Institute of Chicago",
            primary_image=ImageInfo(url="https://example.com/monet.jpg", width=800, height=800),
        )

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=test_image_bytes
            )
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            state_repo = InMemoryStateRepository()
            provider = FakeMuseumProvider([artwork])
            runner = PipelineRunner(
                providers={"artic": provider},  # type: ignore[dict-item]
                state_repo=state_repo,
            )

            request = DiscoveryRequest(museum="artic", limit=1)
            summary = await runner.run(request, http_client=http_client)

        assert summary.fetched == 1
        assert summary.rights_approved == 1
        assert summary.image_valid == 1
        assert summary.high_confidence_classified == 1
        assert summary.board_routed == 1
        assert summary.ready_dry_run_plans == 1
        assert summary.pinterest_writes == 0
        assert summary.errors == 0

        # Validate dry-run plan
        plan = summary.plans[0]
        assert plan.artwork_id == "artic:101"
        assert plan.title == "Water Lilies at Sunset"
        assert plan.movement == ArtMovement.IMPRESSIONISM.value
        assert plan.confidence_level == ConfidenceLevel.HIGH.value
        assert plan.board_name == "Impressionism"
        assert plan.board_id is None  # Never fabricated!
        assert plan.dry_run is True
        assert plan.publishable is False
        assert len(plan.image_sha256) == 64

        # Verify state isolation: dry-run plan is recorded in dry-run storage, NOT as published
        assert await state_repo.artwork_was_published("artic:101") is False
        dry_plan_record = await state_repo.get_dry_run_plan("artic:101")
        assert dry_plan_record is not None
        assert dry_plan_record["title"] == "Water Lilies at Sunset"

    @pytest.mark.asyncio
    async def test_rights_denied_gate(self):
        # Copyrighted artwork
        artwork = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="202"),
            title="Modern Copyrighted Work",
            artist_name="Modern Artist",
            public_domain=False,
            rights_text="© All Rights Reserved",
            museum_name="Art Institute of Chicago",
            primary_image=ImageInfo(url="https://example.com/art.jpg"),
        )

        runner = PipelineRunner(
            providers={"artic": FakeMuseumProvider([artwork])},  # type: ignore[dict-item]
            state_repo=InMemoryStateRepository(),
        )

        summary = await runner.run(DiscoveryRequest(museum="artic", limit=1))
        assert summary.fetched == 1
        assert summary.rights_approved == 0
        assert summary.rights_rejected == 1
        assert summary.ready_dry_run_plans == 0
        assert summary.items[0].decision == PipelineDecision.RIGHTS_DENIED

    @pytest.mark.asyncio
    async def test_missing_image_gate(self):
        # Artwork with no primary or additional images
        artwork = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="303"),
            title="Missing Image Sculpture",
            public_domain=True,
            museum_name="Art Institute of Chicago",
            primary_image=None,
        )

        runner = PipelineRunner(
            providers={"artic": FakeMuseumProvider([artwork])},  # type: ignore[dict-item]
            state_repo=InMemoryStateRepository(),
        )

        summary = await runner.run(DiscoveryRequest(museum="artic", limit=1))
        assert summary.fetched == 1
        assert summary.rights_approved == 1
        assert summary.image_invalid == 1
        assert summary.items[0].decision == PipelineDecision.NO_IMAGE

    @pytest.mark.asyncio
    async def test_duplicate_canonical_id_detected(self, test_image_bytes):
        import httpx

        artwork = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="404"),
            title="Already Published Artwork",
            artist_name="Claude Monet",
            style_raw="Impressionism",
            public_domain=True,
            museum_name="Art Institute of Chicago",
            primary_image=ImageInfo(url="https://example.com/monet.jpg"),
        )

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=test_image_bytes
            )
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            state_repo = InMemoryStateRepository()
            # Mark as already published in repository
            rec = PublicationRecord(artwork_canonical_id="artic:404")
            rec.transition_to(PublicationState.PUBLISHED)
            await state_repo.save_record(rec)

            runner = PipelineRunner(
                providers={"artic": FakeMuseumProvider([artwork])},  # type: ignore[dict-item]
                state_repo=state_repo,
            )

            summary = await runner.run(
                DiscoveryRequest(museum="artic", limit=1), http_client=http_client
            )

        assert summary.duplicates_published == 1
        assert summary.ready_dry_run_plans == 0
        assert summary.items[0].decision == PipelineDecision.ALREADY_PUBLISHED

    @pytest.mark.asyncio
    async def test_duplicate_exact_image_sha256_detected(self, test_image_bytes):
        import hashlib

        import httpx

        image_hash = hashlib.sha256(test_image_bytes).hexdigest()

        # Artwork from Met with different ID but identical image bytes
        artwork = Artwork(
            identity=ArtworkIdentity(source="met", source_object_id="999"),
            title="Cross Source Duplicate",
            artist_name="Claude Monet",
            style_raw="Impressionism",
            public_domain=True,
            museum_name="The Metropolitan Museum of Art",
            primary_image=ImageInfo(url="https://example.com/duplicate.jpg"),
        )

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=test_image_bytes
            )
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            state_repo = InMemoryStateRepository()
            # An earlier record from ArtIC already used this exact image SHA-256
            earlier_rec = PublicationRecord(
                artwork_canonical_id="artic:original_1",
                image_sha256=image_hash,
            )
            await state_repo.save_record(earlier_rec)

            runner = PipelineRunner(
                providers={"met": FakeMuseumProvider([artwork])},  # type: ignore[dict-item]
                state_repo=state_repo,
            )

            summary = await runner.run(
                DiscoveryRequest(museum="met", limit=1), http_client=http_client
            )

        assert summary.duplicates_image_hash == 1
        assert summary.ready_dry_run_plans == 0
        assert summary.items[0].decision == PipelineDecision.DUPLICATE_IMAGE_HASH
        assert "artic:original_1" in summary.items[0].reason

    @pytest.mark.asyncio
    async def test_low_confidence_classification_is_held(self, test_image_bytes):
        """CRITICAL: Low confidence or date-only classification must be HELD, not auto-routed."""
        import httpx

        # Unknown artist + date heuristic only -> weak confidence
        artwork = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="505"),
            title="Unknown Landscape 1880",
            artist_name=None,
            year_start=1880,  # Date alone
            public_domain=True,
            museum_name="Art Institute of Chicago",
            primary_image=ImageInfo(url="https://example.com/landscape.jpg"),
        )

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=test_image_bytes
            )
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            runner = PipelineRunner(
                providers={"artic": FakeMuseumProvider([artwork])},  # type: ignore[dict-item]
                state_repo=InMemoryStateRepository(),
            )
            summary = await runner.run(
                DiscoveryRequest(museum="artic", limit=1), http_client=http_client
            )

        assert summary.held_for_review == 1
        assert summary.high_confidence_classified == 0
        assert summary.ready_dry_run_plans == 0
        assert summary.items[0].decision == PipelineDecision.CLASSIFICATION_UNCERTAIN
        assert "Held for review" in summary.items[0].reason

    @pytest.mark.asyncio
    async def test_item_level_failure_isolation(self, test_image_bytes):
        """One bad item must NOT abort the rest of the batch."""
        import httpx

        # Item 1: bad image response
        art1 = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="item1"),
            title="Bad Image Item",
            public_domain=True,
            museum_name="Art Institute of Chicago",
            primary_image=ImageInfo(url="https://example.com/bad.jpg"),
        )

        # Item 2: valid item
        art2 = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="item2"),
            title="Good Water Lilies",
            artist_name="Claude Monet",
            style_raw="Impressionism",
            public_domain=True,
            museum_name="Art Institute of Chicago",
            primary_image=ImageInfo(url="https://example.com/good.jpg"),
        )

        def mock_handler(request: httpx.Request):
            if "bad.jpg" in str(request.url):
                return httpx.Response(500, content=b"Server error")
            return httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=test_image_bytes
            )

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            runner = PipelineRunner(
                providers={"artic": FakeMuseumProvider([art1, art2])},  # type: ignore[dict-item]
                state_repo=InMemoryStateRepository(),
            )
            summary = await runner.run(
                DiscoveryRequest(museum="artic", limit=2), http_client=http_client
            )

        assert summary.fetched == 2
        # Item 1 failed image validation
        assert summary.items[0].decision == PipelineDecision.IMAGE_INVALID
        # Item 2 succeeded all gates
        assert summary.items[1].decision == PipelineDecision.READY
        assert summary.ready_dry_run_plans == 1
        assert summary.pinterest_writes == 0
