"""Tests for publication state machine, safety defaults, and state repository."""

import pytest
from pydantic import ValidationError

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.domain.publication import (
    REQUIRES_RECONCILIATION,
    TERMINAL_STATES,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.state.repository import InMemoryStateRepository


class TestPublicationState:
    """Publication state machine must support recovery and AMBIGUOUS handling."""

    def test_initial_state_is_discovered(self):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        assert record.state == PublicationState.DISCOVERED

    def test_transition_records_history(self):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.RIGHTS_APPROVED, reason="Public domain")
        assert record.state == PublicationState.RIGHTS_APPROVED
        assert len(record.state_history) == 1
        assert record.state_history[0].from_state == PublicationState.DISCOVERED
        assert record.state_history[0].to_state == PublicationState.RIGHTS_APPROVED

    def test_published_sets_timestamp(self):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        assert record.published_at is None
        record.transition_to(PublicationState.PUBLISHED)
        assert record.published_at is not None

    def test_failed_sets_timestamp(self):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.FAILED, reason="API error")
        assert record.failed_at is not None

    def test_ambiguous_requires_reconciliation(self):
        assert PublicationState.AMBIGUOUS in REQUIRES_RECONCILIATION

    def test_published_is_terminal(self):
        assert PublicationState.PUBLISHED in TERMINAL_STATES

    def test_full_lifecycle(self):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.RIGHTS_APPROVED)
        record.transition_to(PublicationState.CLASSIFIED)
        record.transition_to(PublicationState.BOARD_ASSIGNED)
        record.transition_to(PublicationState.CURATED)
        record.transition_to(PublicationState.PLANNED)
        record.transition_to(PublicationState.RESERVED)
        record.transition_to(PublicationState.PUBLISHING)
        record.transition_to(PublicationState.PUBLISHED)
        assert record.state == PublicationState.PUBLISHED
        assert len(record.state_history) == 8

    def test_ambiguous_lifecycle(self):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHING)
        record.transition_to(PublicationState.AMBIGUOUS, reason="Network timeout")
        assert record.state == PublicationState.AMBIGUOUS

    def test_ambiguous_cannot_transition_to_failed(self):
        """CRITICAL: AMBIGUOUS cannot be silently converted into FAILED."""
        from pinterest_art_bot.domain.publication import InvalidStateTransitionError

        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHING)
        record.transition_to(PublicationState.AMBIGUOUS, reason="Network timeout")

        with pytest.raises(
            InvalidStateTransitionError,
            match="AMBIGUOUS requires definitive reconciliation",
        ):
            record.transition_to(PublicationState.FAILED)

    def test_ambiguous_cannot_be_blindly_retried(self):
        """CRITICAL: AMBIGUOUS cannot be directly retried to PUBLISHING."""
        from pinterest_art_bot.domain.publication import InvalidStateTransitionError

        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHING)
        record.transition_to(PublicationState.AMBIGUOUS, reason="Network timeout")

        with pytest.raises(InvalidStateTransitionError):
            record.transition_to(PublicationState.PUBLISHING)

    def test_ambiguous_can_transition_to_reconciled(self):
        """AMBIGUOUS can be transitioned to RECONCILED after definitive check."""
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHING)
        record.transition_to(PublicationState.AMBIGUOUS, reason="Network timeout")
        record.transition_to(
            PublicationState.RECONCILED, reason="Definitive board scan confirmed no pin"
        )
        assert record.state == PublicationState.RECONCILED

    def test_terminal_state_cannot_transition(self):
        """Terminal states like PUBLISHED cannot transition to another state."""
        from pinterest_art_bot.domain.publication import InvalidStateTransitionError

        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHED)

        with pytest.raises(InvalidStateTransitionError, match="terminal state"):
            record.transition_to(PublicationState.DISCOVERED)


class TestSafetyDefaults:
    """Production safety switches must default to OFF."""

    def test_publish_defaults_false(self):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.publish_to_pinterest is False

    def test_auto_create_boards_defaults_false(self):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.pinterest_auto_create_boards is False

    def test_production_schedule_defaults_false(self):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.pinterest_production_schedule_enabled is False

    def test_cannot_publish_without_token(self):
        """Enabling the master switch without a token must fail at construction."""
        with pytest.raises(ValidationError, match="requires PINTEREST_ACCESS_TOKEN"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                publish_to_pinterest=True,
            )

    def test_cannot_publish_without_switch(self):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            pinterest_access_token="test-token",
        )
        assert settings.can_publish is False  # Switch is off

    def test_cannot_weaken_rights_gate_while_publishing(self):
        """The rights gate cannot be relaxed while publishing is enabled."""
        with pytest.raises(ValidationError, match="RIGHTS_ENFORCE_STRICT"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                publish_to_pinterest=True,
                pinterest_access_token="test-token",
                rights_enforce_strict=False,
            )

    def test_board_creation_requires_publishing(self):
        with pytest.raises(ValidationError, match="PINTEREST_AUTO_CREATE_BOARDS"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                pinterest_auto_create_boards=True,
            )

    def test_can_publish_with_both(self):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            publish_to_pinterest=True,
            pinterest_access_token="test-token",
        )
        assert settings.can_publish is True
        assert settings.can_create_boards is False  # board creation still off
        assert settings.can_schedule is False


class TestStateRepository:
    """In-memory state repository for testing."""

    @pytest.fixture
    def repo(self):
        return InMemoryStateRepository()

    @pytest.mark.asyncio
    async def test_save_and_get(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        await repo.save_record(record)
        retrieved = await repo.get_record("met:436535")
        assert retrieved is not None
        assert retrieved.artwork_canonical_id == "met:436535"

    @pytest.mark.asyncio
    async def test_artwork_not_found(self, repo):
        result = await repo.get_record("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_artwork_was_published(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHED)
        await repo.save_record(record)
        assert await repo.artwork_was_published("met:436535") is True

    @pytest.mark.asyncio
    async def test_not_published_yet(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        await repo.save_record(record)
        assert await repo.artwork_was_published("met:436535") is False

    @pytest.mark.asyncio
    async def test_reserve_artwork(self, repo):
        reserved = await repo.reserve_artwork("met:436535", "run-1")
        assert reserved is True

    @pytest.mark.asyncio
    async def test_double_reserve_same_run(self, repo):
        await repo.reserve_artwork("met:436535", "run-1")
        reserved = await repo.reserve_artwork("met:436535", "run-1")
        assert reserved is True  # Same run, OK

    @pytest.mark.asyncio
    async def test_double_reserve_different_run(self, repo):
        await repo.reserve_artwork("met:436535", "run-1")
        reserved = await repo.reserve_artwork("met:436535", "run-2")
        assert reserved is False  # Different run, blocked

    @pytest.mark.asyncio
    async def test_release_reservation(self, repo):
        await repo.reserve_artwork("met:436535", "run-1")
        await repo.release_reservation("met:436535", "run-1")
        reserved = await repo.reserve_artwork("met:436535", "run-2")
        assert reserved is True  # Released, so new run can reserve

    @pytest.mark.asyncio
    async def test_mark_ambiguous(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHING)
        await repo.save_record(record)
        await repo.mark_ambiguous("met:436535", "Network timeout")
        retrieved = await repo.get_record("met:436535")
        assert retrieved is not None
        assert retrieved.state == PublicationState.AMBIGUOUS

    @pytest.mark.asyncio
    async def test_image_hash_dedup(self, repo):
        record = PublicationRecord(
            artwork_canonical_id="met:436535",
            image_sha256="abc123",
        )
        await repo.save_record(record)
        found = await repo.check_image_hash("abc123")
        assert found is not None
        assert found.artwork_canonical_id == "met:436535"

    @pytest.mark.asyncio
    async def test_image_hash_not_found(self, repo):
        found = await repo.check_image_hash("nonexistent")
        assert found is None

    @pytest.mark.asyncio
    async def test_cannot_reserve_published(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:436535")
        record.transition_to(PublicationState.PUBLISHED)
        await repo.save_record(record)
        reserved = await repo.reserve_artwork("met:436535", "run-2")
        assert reserved is False


class TestSqliteStateRepository:
    """SQLite development repository must implement full state semantics safely."""

    @pytest.fixture
    def sqlite_repo(self):
        from pinterest_art_bot.state.sqlite import SqliteStateRepository

        repo = SqliteStateRepository(":memory:")
        yield repo
        repo.close()

    @pytest.mark.asyncio
    async def test_sqlite_save_and_retrieve(self, sqlite_repo):
        record = PublicationRecord(
            artwork_canonical_id="artic:12345",
            run_id="run-test",
        )
        record.transition_to(PublicationState.RIGHTS_APPROVED)
        await sqlite_repo.save_record(record)

        loaded = await sqlite_repo.get_record("artic:12345")
        assert loaded is not None
        assert loaded.artwork_canonical_id == "artic:12345"
        assert loaded.state == PublicationState.RIGHTS_APPROVED

    @pytest.mark.asyncio
    async def test_dry_run_never_marks_published(self, sqlite_repo):
        """CRITICAL: Dry-run storage must NOT make an artwork appear published."""
        plan_data = {
            "artwork_id": "artic:999",
            "title": "Dry Run Painting",
            "dry_run": True,
            "publishable": False,
        }
        await sqlite_repo.record_dry_run_plan("artic:999", "run-dry", plan_data)

        # It must NOT be marked as published
        assert await sqlite_repo.artwork_was_published("artic:999") is False

        # But dry-run plan is retrievable
        stored_plan = await sqlite_repo.get_dry_run_plan("artic:999")
        assert stored_plan is not None
        assert stored_plan["title"] == "Dry Run Painting"

    @pytest.mark.asyncio
    async def test_sqlite_image_sha256_dedup(self, sqlite_repo):
        record = PublicationRecord(
            artwork_canonical_id="met:1001",
            image_sha256="deadbeef" * 8,
        )
        await sqlite_repo.save_record(record)

        found = await sqlite_repo.check_image_hash("deadbeef" * 8)
        assert found is not None
        assert found.artwork_canonical_id == "met:1001"

    @pytest.mark.asyncio
    async def test_sqlite_ambiguous_cannot_be_reserved(self, sqlite_repo):
        record = PublicationRecord(artwork_canonical_id="met:555")
        record.transition_to(PublicationState.PUBLISHING)
        await sqlite_repo.save_record(record)
        await sqlite_repo.mark_ambiguous("met:555", "Network dropped")

        # Ambiguous record must reject reservation
        reserved = await sqlite_repo.reserve_artwork("met:555", "run-next")
        assert reserved is False
