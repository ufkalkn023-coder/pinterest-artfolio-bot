"""Tests for ambiguous-write reconciliation.

AMBIGUOUS is the only state with a dedicated recovery path, and it must never
turn into a blind retry.
"""

from pinterest_art_bot.domain.publication import (
    BoardTarget,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.pinterest.client import StubPinterestClient
from pinterest_art_bot.pinterest.models import CreateBoardRequest, CreatePinRequest
from pinterest_art_bot.pinterest.reconciliation import (
    AmbiguousWriteReconciler,
    ReconciliationOutcome,
)
from pinterest_art_bot.state.repository import InMemoryStateRepository


async def ambiguous_record(
    repo: InMemoryStateRepository,
    *,
    artwork_id: str = "met:436535",
    board_id: str | None = "1000",
    link: str | None = "https://www.metmuseum.org/art/436535",
    image_url: str | None = "https://images.metmuseum.org/436535.jpg",
    pin_id: str | None = None,
) -> PublicationRecord:
    """Persist a record parked in AMBIGUOUS, as a failed write would leave it."""
    record = PublicationRecord(artwork_canonical_id=artwork_id, run_id="run_1")
    record.board_target = BoardTarget(
        board_key="impressionism",
        board_name="Impressionism",
        board_id=board_id,
    )
    record.link = link
    record.image_url_used = image_url
    record.pin_id = pin_id
    record.transition_to(PublicationState.PUBLISHING, reason="write started")
    record.transition_to(PublicationState.AMBIGUOUS, reason="network timeout")
    await repo.save_record(record)
    return record


class TestKnownPinId:
    async def test_existing_pin_resolves_to_published(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        pin = await client.create_pin(CreatePinRequest(board_id="1000", title="t", description="d"))
        await ambiguous_record(repo, pin_id=pin.id)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()

        assert summary.examined == 1
        assert summary.published == 1
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.state == PublicationState.PUBLISHED

    async def test_absent_pin_resolves_to_reconciled(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        await ambiguous_record(repo, pin_id="999999")

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()

        assert summary.reconciled == 1
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.state == PublicationState.RECONCILED


class TestBoardScan:
    async def test_pin_found_on_board_by_link(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        pin = await client.create_pin(
            CreatePinRequest(
                board_id=board.id,
                title="t",
                description="d",
                link="https://www.metmuseum.org/art/436535",
            )
        )
        await ambiguous_record(repo, board_id=board.id, pin_id=None)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()

        assert summary.published == 1
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.pin_id == pin.id
        assert record.state == PublicationState.PUBLISHED

    async def test_pin_found_on_board_by_media_url(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        pin = await client.create_pin(
            CreatePinRequest(
                board_id=board.id,
                title="t",
                description="d",
                media_source_url="https://images.metmuseum.org/436535.jpg",
            )
        )
        await ambiguous_record(repo, board_id=board.id, pin_id=None)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()
        assert summary.published == 1
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.pin_id == pin.id

    async def test_definitive_empty_scan_resolves_to_reconciled(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        await client.create_pin(
            CreatePinRequest(
                board_id=board.id,
                title="unrelated",
                description="d",
                link="https://example.com/other",
            )
        )
        await ambiguous_record(repo, board_id=board.id, pin_id=None)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()

        assert summary.reconciled == 1
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.state == PublicationState.RECONCILED


class TestManualReview:
    async def test_missing_board_requires_manual_review(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        await ambiguous_record(repo, board_id=None, pin_id=None)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()

        assert summary.manual_review_required == 1
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.state == PublicationState.AMBIGUOUS

    async def test_missing_link_requires_manual_review(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        await ambiguous_record(repo, link=None, image_url=None, pin_id=None)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()
        assert summary.manual_review_required == 1

    async def test_non_ambiguous_record_is_skipped(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.transition_to(PublicationState.FAILED, reason="definitive failure")
        await repo.save_record(record)

        reconciler = AmbiguousWriteReconciler(client, repo)
        result = await reconciler.reconcile_record(record)

        assert result.outcome == ReconciliationOutcome.SKIPPED
        assert result.state_after == PublicationState.FAILED


class TestReconcileAllScope:
    async def test_only_ambiguous_records_are_examined(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        await ambiguous_record(repo, artwork_id="met:1")
        await ambiguous_record(repo, artwork_id="met:2")

        published = PublicationRecord(artwork_canonical_id="met:3")
        published.transition_to(PublicationState.PUBLISHED)
        await repo.save_record(published)

        summary = await AmbiguousWriteReconciler(client, repo).reconcile_all()
        assert summary.examined == 2
        assert len(summary.results) == 2


class TestWorkBudget:
    """Reconciliation must be bounded: a backlog cannot drain the API quota."""

    async def test_record_count_is_capped(self):
        repo = InMemoryStateRepository()
        for i in range(5):
            await ambiguous_record(repo, artwork_id=f"met:{i}", pin_id=None)

        client = StubPinterestClient()
        summary = await AmbiguousWriteReconciler(client, repo, max_records=2).reconcile_all()

        assert summary.examined == 2
        assert summary.remaining == 3
        assert summary.incomplete is True

    async def test_truncated_scan_never_claims_absence(self):
        """A scan that hit its budget must not conclude the Pin does not exist."""
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        # A board big enough to exhaust a one-page budget.
        for i in range(100):
            await client.create_pin(
                CreatePinRequest(
                    board_id=board.id,
                    title=f"other {i}",
                    description="d",
                    link=f"https://example.com/other-{i}",
                )
            )
        await ambiguous_record(repo, board_id=board.id, pin_id=None)

        reconciler = AmbiguousWriteReconciler(client, repo, max_pages_per_board=1)
        result = await reconciler.reconcile_record(await repo.get_record("met:436535"))

        assert result.outcome == ReconciliationOutcome.MANUAL_REVIEW_REQUIRED
        record = await repo.get_record("met:436535")
        assert record is not None
        assert record.state == PublicationState.AMBIGUOUS, "must stay ambiguous"

    async def test_larger_budget_can_prove_absence(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        for i in range(100):
            await client.create_pin(
                CreatePinRequest(
                    board_id=board.id,
                    title=f"other {i}",
                    description="d",
                    link=f"https://example.com/other-{i}",
                )
            )
        await ambiguous_record(repo, board_id=board.id, pin_id=None)

        reconciler = AmbiguousWriteReconciler(client, repo, max_pages_per_board=10)
        result = await reconciler.reconcile_record(await repo.get_record("met:436535"))
        assert result.outcome == ReconciliationOutcome.RECONCILED

    async def test_complete_scan_still_resolves(self):
        repo = InMemoryStateRepository()
        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Impressionism"))
        await ambiguous_record(repo, board_id=board.id, pin_id=None)

        reconciler = AmbiguousWriteReconciler(client, repo, max_pages_per_board=10)
        result = await reconciler.reconcile_record(await repo.get_record("met:436535"))
        assert result.outcome == ReconciliationOutcome.RECONCILED

    async def test_defaults_are_sane(self):
        repo = InMemoryStateRepository()
        reconciler = AmbiguousWriteReconciler(StubPinterestClient(), repo)
        summary = await reconciler.reconcile_all()
        assert summary.examined == 0
        assert summary.incomplete is False
