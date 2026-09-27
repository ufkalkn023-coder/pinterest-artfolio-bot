"""Tests for the SQLite state repository (durability, concurrency, GC)."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pinterest_art_bot.domain.publication import (
    BoardTarget,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.state.factory import create_state_repository
from pinterest_art_bot.state.sqlite import SqliteStateRepository


def published_record(artwork_id: str = "met:1", **overrides) -> PublicationRecord:
    """A record in the PUBLISHED terminal state."""
    record = PublicationRecord(artwork_canonical_id=artwork_id, **overrides)
    record.image_sha256 = overrides.get("image_sha256") or f"sha-{artwork_id}"
    record.transition_to(PublicationState.PUBLISHED)
    return record


@pytest.fixture
def repo(tmp_path) -> SqliteStateRepository:
    """A repository backed by a real file."""
    return SqliteStateRepository(tmp_path / "state.db")


class TestDurability:
    async def test_records_survive_reopen(self, tmp_path):
        path = tmp_path / "state.db"
        first = SqliteStateRepository(path)
        await first.save_record(published_record("met:436535"))
        first.close()

        second = SqliteStateRepository(path)
        record = await second.get_record("met:436535")
        assert record is not None
        assert record.state == PublicationState.PUBLISHED
        assert await second.artwork_was_published("met:436535") is True
        second.close()

    async def test_parent_directory_is_created(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "state.db"
        repo = SqliteStateRepository(path)
        assert Path(path).exists()
        await repo.save_record(published_record())
        repo.close()

    async def test_schema_is_idempotent(self, tmp_path):
        path = tmp_path / "state.db"
        SqliteStateRepository(path).close()
        SqliteStateRepository(path).close()  # must not raise or wipe data

    def test_wal_is_enabled_for_file_databases(self, tmp_path):
        path = tmp_path / "state.db"
        repo = SqliteStateRepository(path)
        mode = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]
        repo.close()
        assert mode.lower() == "wal"


class TestPublicationState:
    async def test_save_and_load_round_trip(self, repo):
        record = published_record("met:1")
        record.board_target = BoardTarget(
            board_key="impressionism", board_name="Impressionism", board_id="55"
        )
        record.movement_classification = None
        await repo.save_record(record)

        loaded = await repo.get_record("met:1")
        assert loaded is not None
        assert loaded.board_target is not None
        assert loaded.board_target.board_id == "55"
        assert loaded.pin_id is None

    async def test_image_hash_index_is_populated(self, repo):
        record = published_record("met:1", image_sha256="abc123")
        await repo.save_record(record)

        found = await repo.check_image_hash("abc123")
        assert found is not None
        assert found.artwork_canonical_id == "met:1"
        assert await repo.check_image_hash("missing") is None

    async def test_records_by_state(self, repo):
        await repo.save_record(published_record("met:1"))
        failed = PublicationRecord(artwork_canonical_id="met:2")
        failed.transition_to(PublicationState.FAILED, reason="boom")
        await repo.save_record(failed)

        published = await repo.get_records_by_state(PublicationState.PUBLISHED)
        assert [r.artwork_canonical_id for r in published] == ["met:1"]
        assert len(await repo.get_records_by_state(PublicationState.FAILED)) == 1

    async def test_list_records_filters_and_limits(self, repo):
        for i in range(5):
            await repo.save_record(published_record(f"met:{i}"))
        ambiguous = PublicationRecord(artwork_canonical_id="met:9")
        ambiguous.transition_to(PublicationState.FAILED, reason="x")
        ambiguous.transition_to(PublicationState.RESERVED, reason="y")
        ambiguous.transition_to(PublicationState.PUBLISHING, reason="z")
        ambiguous.transition_to(PublicationState.AMBIGUOUS, reason="timeout")
        await repo.save_record(ambiguous)

        assert len(await repo.list_records(limit=3)) == 3
        ambiguous_only = await repo.list_records(states=[PublicationState.AMBIGUOUS], limit=10)
        assert [r.artwork_canonical_id for r in ambiguous_only] == ["met:9"]

    async def test_count_published_since(self, repo):
        await repo.save_record(published_record("met:1"))
        yesterday = datetime.now(UTC) - timedelta(days=1)

        assert await repo.count_published_since(yesterday) == 1
        assert await repo.count_published_since(datetime.now(UTC) + timedelta(hours=1)) == 0


class TestReservations:
    async def test_reservation_blocks_other_runs(self, repo):
        assert await repo.reserve_artwork("met:1", "run_a") is True
        assert await repo.reserve_artwork("met:1", "run_b") is False

    async def test_same_run_is_idempotent(self, repo):
        assert await repo.reserve_artwork("met:1", "run_a") is True
        assert await repo.reserve_artwork("met:1", "run_a") is True

    async def test_published_artworks_are_not_reservable(self, repo):
        await repo.save_record(published_record("met:1"))
        assert await repo.reserve_artwork("met:1", "run_a") is False

    async def test_ambiguous_artworks_are_not_reservable(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.transition_to(PublicationState.RESERVED, reason="r")
        record.transition_to(PublicationState.PUBLISHING, reason="p")
        record.transition_to(PublicationState.AMBIGUOUS, reason="timeout")
        await repo.save_record(record)
        assert await repo.reserve_artwork("met:1", "run_a") is False

    async def test_release_allows_retry(self, repo):
        await repo.reserve_artwork("met:1", "run_a")
        await repo.release_reservation("met:1", "run_a")
        assert await repo.reserve_artwork("met:1", "run_b") is True

    async def test_release_by_other_run_is_ignored(self, repo):
        await repo.reserve_artwork("met:1", "run_a")
        await repo.release_reservation("met:1", "run_b")
        assert await repo.reserve_artwork("met:1", "run_b") is False

    async def test_purge_expired_reservations(self, repo):
        await repo.reserve_artwork("met:1", "run_a")
        assert await repo.purge_expired_reservations(3600) == 0
        assert await repo.purge_expired_reservations(0) == 0
        # A TTL of one second is not yet expired, so nothing is dropped.
        assert await repo.purge_expired_reservations(1) == 0


class TestAmbiguous:
    async def test_mark_ambiguous_updates_state(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.transition_to(PublicationState.RESERVED, reason="r")
        record.transition_to(PublicationState.PUBLISHING, reason="p")
        await repo.save_record(record)

        await repo.mark_ambiguous("met:1", "network timeout")

        loaded = await repo.get_record("met:1")
        assert loaded is not None
        assert loaded.state == PublicationState.AMBIGUOUS
        assert loaded.error_message == "network timeout"

    async def test_mark_ambiguous_without_record_is_safe(self, repo):
        await repo.mark_ambiguous("met:unknown", "timeout")
        assert await repo.get_record("met:unknown") is None


class TestPerceptualHashes:
    async def test_round_trip(self, repo):
        await repo.record_perceptual_hash("00ff00ff00ff00ff", "met:1")
        pairs = await repo.list_perceptual_hashes()
        assert ("00ff00ff00ff00ff", "met:1") in pairs

    async def test_duplicates_are_ignored(self, repo):
        await repo.record_perceptual_hash("00ff00ff00ff00ff", "met:1")
        await repo.record_perceptual_hash("00ff00ff00ff00ff", "met:1")
        assert len(await repo.list_perceptual_hashes()) == 1

    async def test_hash_recorded_with_published_record(self, repo):
        record = published_record("met:1")
        record.image_perceptual_hash = "abcdabcdabcdabcd"
        await repo.save_record(record)
        assert ("abcdabcdabcdabcd", "met:1") in await repo.list_perceptual_hashes()


class TestDryRunStorage:
    async def test_plans_are_stored_separately(self, repo):
        plan = {"artwork_id": "met:1", "dry_run": True, "publishable": False}
        await repo.record_dry_run_plan("met:1", "run_1", plan)

        assert await repo.get_dry_run_plan("met:1") == plan
        # Dry-run storage must not create publication history.
        assert await repo.artwork_exists("met:1") is False

    async def test_plans_are_overwritten_per_artwork(self, repo):
        await repo.record_dry_run_plan("met:1", "run_1", {"v": 1})
        await repo.record_dry_run_plan("met:1", "run_2", {"v": 2})
        assert await repo.get_dry_run_plan("met:1") == {"v": 2}

    async def test_missing_plan_returns_none(self, repo):
        assert await repo.get_dry_run_plan("met:nope") is None


class TestConcurrency:
    async def test_concurrent_writes_are_serialised(self, repo):
        """SQLite access is off the event loop and serialised behind a lock."""
        records = [published_record(f"met:{i}") for i in range(25)]

        await asyncio.gather(*(repo.save_record(record) for record in records))

        for i in range(25):
            loaded = await repo.get_record(f"met:{i}")
            assert loaded is not None
            assert loaded.state == PublicationState.PUBLISHED

    async def test_concurrent_reservations_grant_exactly_one(self, repo):
        results = await asyncio.gather(
            *(repo.reserve_artwork("met:contended", f"run_{i}") for i in range(10))
        )
        assert sum(1 for granted in results if granted) == 1


class TestFactory:
    def test_builds_file_repository_from_settings(self, tmp_path):
        from pinterest_art_bot.config.settings import Settings

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, state_db_path=str(tmp_path / "custom.db")
        )
        repo = create_state_repository(settings)
        assert Path(settings.state_db_path).exists()
        repo.close()

    def test_in_memory_flag(self):
        from pinterest_art_bot.config.settings import Settings

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        repo = create_state_repository(settings, in_memory=True)
        assert isinstance(repo, SqliteStateRepository)
        repo.close()

    def test_satisfies_protocol(self, repo):
        from pinterest_art_bot.state.repository import StateRepository

        assert isinstance(repo, StateRepository)


def test_schema_version_is_recorded(tmp_path):
    path = tmp_path / "state.db"
    SqliteStateRepository(path).close()
    conn = sqlite3.connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version >= 1


class TestDiscoveryCursor:
    async def test_round_trip_and_persistence(self, tmp_path):
        path = tmp_path / "state.db"
        first = SqliteStateRepository(path)
        assert await first.get_cursor("discovery:met") is None
        await first.set_cursor("discovery:met", 250)
        first.close()

        second = SqliteStateRepository(path)
        assert await second.get_cursor("discovery:met") == 250
        await second.set_cursor("discovery:met", 400)
        assert await second.get_cursor("discovery:met") == 400
        second.close()

    async def test_cursors_are_independent(self, repo):
        await repo.set_cursor("a", 1)
        await repo.set_cursor("b", 2)
        assert await repo.get_cursor("a") == 1
        assert await repo.get_cursor("b") == 2

    async def test_in_memory_repository_cursor(self):
        from pinterest_art_bot.state.repository import InMemoryStateRepository

        memory = InMemoryStateRepository()
        assert await memory.get_cursor("x") is None
        await memory.set_cursor("x", 7)
        assert await memory.get_cursor("x") == 7


class TestDryRunPlanListing:
    async def test_plans_are_listed_newest_first(self, repo):
        await repo.record_dry_run_plan("met:1", "run_a", {"artwork_id": "met:1"})
        await repo.record_dry_run_plan("met:2", "run_b", {"artwork_id": "met:2"})

        plans = await repo.list_dry_run_plans()
        assert {plan["artwork_id"] for plan in plans} == {"met:1", "met:2"}

    async def test_limit_is_respected(self, repo):
        for i in range(5):
            await repo.record_dry_run_plan(f"met:{i}", "run", {"artwork_id": f"met:{i}"})
        assert len(await repo.list_dry_run_plans(limit=2)) == 2

    async def test_in_memory_listing(self):
        from pinterest_art_bot.state.repository import InMemoryStateRepository

        memory = InMemoryStateRepository()
        await memory.record_dry_run_plan("met:1", "run", {"artwork_id": "met:1"})
        plans = await memory.list_dry_run_plans()
        assert plans[0]["artwork_id"] == "met:1"


class TestWorkIdentities:
    async def test_round_trip(self, repo):
        from pinterest_art_bot.domain.work_identity import WorkIdentity

        identity = WorkIdentity(
            artist_tokens=frozenset({"degas", "edgar"}),
            title_tokens=frozenset({"dance", "class"}),
            year=1874,
        )
        await repo.record_work_identity("key-1", "met:1", identity)

        stored = await repo.list_work_identities()
        assert len(stored) == 1
        key, artwork_id, loaded = stored[0]
        assert key == "key-1"
        assert artwork_id == "met:1"
        assert loaded == identity

    async def test_duplicates_are_ignored(self, repo):
        from pinterest_art_bot.domain.work_identity import WorkIdentity

        identity = WorkIdentity(
            artist_tokens=frozenset({"degas"}), title_tokens=frozenset({"dance", "class"})
        )
        await repo.record_work_identity("key-1", "met:1", identity)
        await repo.record_work_identity("key-1", "met:1", identity)
        assert len(await repo.list_work_identities()) == 1

    async def test_survives_restart(self, tmp_path):
        from pinterest_art_bot.domain.work_identity import WorkIdentity

        path = tmp_path / "state.db"
        identity = WorkIdentity(
            artist_tokens=frozenset({"monet", "claude"}),
            title_tokens=frozenset({"water", "lilies"}),
            year=1906,
        )
        first = SqliteStateRepository(path)
        await first.record_work_identity("key-9", "artic:1", identity)
        first.close()

        second = SqliteStateRepository(path)
        stored = await second.list_work_identities()
        assert stored[0][0] == "key-9"
        assert stored[0][2].year == 1906
        assert stored[0][2].artist_tokens == frozenset({"monet", "claude"})
        second.close()

    async def test_in_memory_repository(self):
        from pinterest_art_bot.domain.work_identity import WorkIdentity
        from pinterest_art_bot.state.repository import InMemoryStateRepository

        memory = InMemoryStateRepository()
        identity = WorkIdentity(
            artist_tokens=frozenset({"degas"}), title_tokens=frozenset({"dance", "class"})
        )
        await memory.record_work_identity("k", "met:1", identity)
        assert len(await memory.list_work_identities()) == 1


class TestRunHistory:
    async def test_run_is_persisted(self, repo):
        await repo.save_run(
            "run_1",
            {
                "museum": "met",
                "dry_run": True,
                "fetched": 10,
                "published": 2,
                "errors": 1,
                "finished_at": "2026-01-01T00:00:00+00:00",
            },
        )
        runs = await repo.list_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run_1"
        assert runs[0]["museum"] == "met"
        assert runs[0]["dry_run"] is True
        assert runs[0]["published"] == 2
        assert runs[0]["errors"] == 1

    async def test_runs_are_returned_newest_first(self, repo):
        for i in range(3):
            await repo.save_run(
                f"run_{i}",
                {
                    "museum": "met",
                    "dry_run": True,
                    "finished_at": f"2026-01-0{i + 1}T00:00:00+00:00",
                },
            )
        runs = await repo.list_runs()
        assert [run["run_id"] for run in runs] == ["run_2", "run_1", "run_0"]

    async def test_limit_is_respected(self, repo):
        for i in range(5):
            await repo.save_run(f"run_{i}", {"museum": "met", "dry_run": True})
        assert len(await repo.list_runs(limit=2)) == 2

    async def test_survives_restart(self, tmp_path):
        path = tmp_path / "state.db"
        first = SqliteStateRepository(path)
        await first.save_run("run_1", {"museum": "cleveland", "dry_run": False, "published": 5})
        first.close()

        second = SqliteStateRepository(path)
        runs = await second.list_runs()
        assert runs[0]["museum"] == "cleveland"
        assert runs[0]["dry_run"] is False
        assert runs[0]["published"] == 5
        second.close()

    async def test_in_memory_repository(self):
        from pinterest_art_bot.state.repository import InMemoryStateRepository

        memory = InMemoryStateRepository()
        await memory.save_run("run_a", {"museum": "met", "dry_run": True, "fetched": 3})
        runs = await memory.list_runs()
        assert runs[0]["run_id"] == "run_a"
        assert runs[0]["fetched"] == 3


class TestPerBoardActivity:
    async def test_counts_per_board(self, repo):
        from pinterest_art_bot.domain.publication import BoardTarget

        for index, board in enumerate(["impressionism", "impressionism", "baroque"]):
            record = published_record(f"met:{index}")
            record.board_target = BoardTarget(
                board_key=board, board_name=board.title(), board_id=str(index)
            )
            await repo.save_record(record)

        since = datetime.now(UTC) - timedelta(days=1)
        counts = await repo.count_published_per_board_since(since)
        assert counts == {"impressionism": 2, "baroque": 1}

    async def test_excludes_older_publications(self, repo):
        from pinterest_art_bot.domain.publication import BoardTarget

        record = published_record("met:1")
        record.board_target = BoardTarget(
            board_key="impressionism", board_name="Impressionism", board_id="1"
        )
        record.published_at = datetime.now(UTC) - timedelta(days=3)
        await repo.save_record(record)

        since = datetime.now(UTC) - timedelta(days=1)
        assert await repo.count_published_per_board_since(since) == {}

    async def test_ignores_records_without_a_board(self, repo):
        await repo.save_record(published_record("met:1"))
        since = datetime.now(UTC) - timedelta(days=1)
        assert await repo.count_published_per_board_since(since) == {}

    async def test_last_published_per_board(self, repo):
        from pinterest_art_bot.domain.publication import BoardTarget

        older = published_record("met:1")
        older.board_target = BoardTarget(
            board_key="impressionism", board_name="Impressionism", board_id="1"
        )
        older.published_at = datetime.now(UTC) - timedelta(days=2)
        await repo.save_record(older)

        newer = published_record("met:2")
        newer.board_target = BoardTarget(
            board_key="impressionism", board_name="Impressionism", board_id="1"
        )
        newer.published_at = datetime.now(UTC)
        await repo.save_record(newer)

        latest = await repo.last_published_at_per_board()
        assert list(latest) == ["impressionism"]
        assert latest["impressionism"] == newer.published_at

    async def test_survives_restart(self, tmp_path):
        from pinterest_art_bot.domain.publication import BoardTarget

        path = tmp_path / "state.db"
        first = SqliteStateRepository(path)
        record = published_record("met:1")
        record.board_target = BoardTarget(
            board_key="baroque", board_name="Baroque Art", board_id="9"
        )
        await first.save_record(record)
        first.close()

        second = SqliteStateRepository(path)
        since = datetime.now(UTC) - timedelta(days=1)
        assert await second.count_published_per_board_since(since) == {"baroque": 1}
        assert list(await second.last_published_at_per_board()) == ["baroque"]
        second.close()

    async def test_in_memory_repository(self):
        from pinterest_art_bot.domain.publication import BoardTarget
        from pinterest_art_bot.state.repository import InMemoryStateRepository

        memory = InMemoryStateRepository()
        record = published_record("met:1")
        record.board_target = BoardTarget(
            board_key="baroque", board_name="Baroque Art", board_id="9"
        )
        await memory.save_record(record)
        since = datetime.now(UTC) - timedelta(days=1)
        assert await memory.count_published_per_board_since(since) == {"baroque": 1}
        assert list(await memory.last_published_at_per_board()) == ["baroque"]


class TestRetention:
    async def test_run_history_is_purged_by_age(self, repo):
        await repo.save_run(
            "old", {"museum": "met", "dry_run": True, "finished_at": "2020-01-01T00:00:00+00:00"}
        )
        await repo.save_run("new", {"museum": "met", "dry_run": True})
        assert await repo.purge_run_history(0) == 0
        assert len(await repo.list_runs()) == 2

        removed = await repo.purge_run_history(1)
        assert removed == 1
        assert [run["run_id"] for run in await repo.list_runs()] == ["new"]

    async def test_purge_is_disabled_with_zero_retention(self, repo):
        await repo.save_run(
            "keep", {"museum": "met", "dry_run": True, "finished_at": "2020-01-01T00:00:00+00:00"}
        )
        assert await repo.purge_run_history(0) == 0
        assert len(await repo.list_runs()) == 1


class TestRejectedFingerprintRetention:
    async def _seed_rejected(self, repo):
        from pinterest_art_bot.domain.publication import PublicationRecord
        from pinterest_art_bot.domain.work_identity import WorkIdentity

        record = PublicationRecord(artwork_canonical_id="met:denied")
        record.transition_to(PublicationState.RIGHTS_DENIED, reason="unknown rights")
        record.image_sha256 = "sha-denied"
        record.image_perceptual_hash = "phash-denied"
        record.discovered_at = datetime.now(UTC) - timedelta(days=365)
        await repo.save_record(record)

        identity = WorkIdentity(
            artist_tokens=frozenset({"degas"}), title_tokens=frozenset({"dance", "class"})
        )
        await repo.record_work_identity("key-denied", "met:denied", identity)
        return record

    async def test_rejected_fingerprints_are_purged(self, repo):
        await self._seed_rejected(repo)
        removed = await repo.purge_rejected_fingerprints(30)
        assert removed == 3  # exact hash + perceptual hash + work identity
        assert await repo.check_image_hash("sha-denied") is None
        assert await repo.list_work_identities() == []

    async def test_published_artworks_keep_their_fingerprints(self, repo):
        """A lost fingerprint would allow a double publish."""
        record = published_record("met:live")
        record.discovered_at = datetime.now(UTC) - timedelta(days=365)
        await repo.save_record(record)

        assert await repo.purge_rejected_fingerprints(30) == 0
        assert await repo.check_image_hash("sha-met:live") is not None

    async def test_failed_artworks_keep_their_fingerprints(self, repo):
        """FAILED stays retryable, so it must stay deduplicated."""
        from pinterest_art_bot.domain.publication import PublicationRecord

        record = PublicationRecord(artwork_canonical_id="met:failed")
        record.image_sha256 = "sha-failed"
        record.transition_to(PublicationState.FAILED, reason="definitive failure")
        record.discovered_at = datetime.now(UTC) - timedelta(days=365)
        await repo.save_record(record)

        assert await repo.purge_rejected_fingerprints(30) == 0
        assert await repo.check_image_hash("sha-failed") is not None

    async def test_ambiguous_artworks_keep_their_fingerprints(self, repo):
        from pinterest_art_bot.domain.publication import PublicationRecord

        record = PublicationRecord(artwork_canonical_id="met:amb")
        record.image_sha256 = "sha-amb"
        for state in (
            PublicationState.RESERVED,
            PublicationState.PUBLISHING,
            PublicationState.AMBIGUOUS,
        ):
            record.transition_to(state, reason="x")
        record.discovered_at = datetime.now(UTC) - timedelta(days=365)
        await repo.save_record(record)

        assert await repo.purge_rejected_fingerprints(30) == 0
        assert await repo.check_image_hash("sha-amb") is not None

    async def test_recent_rejected_records_are_kept(self, repo):
        from pinterest_art_bot.domain.publication import PublicationRecord

        record = PublicationRecord(artwork_canonical_id="met:recent")
        record.image_sha256 = "sha-recent"
        record.transition_to(PublicationState.RIGHTS_DENIED, reason="unknown rights")
        await repo.save_record(record)

        assert await repo.purge_rejected_fingerprints(30) == 0
        assert await repo.check_image_hash("sha-recent") is not None

    async def test_counts_do_not_load_rows(self, repo):
        from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState

        for i in range(3):
            record = PublicationRecord(artwork_canonical_id=f"met:{i}")
            record.transition_to(PublicationState.RIGHTS_DENIED, reason="no")
            await repo.save_record(record)

        assert await repo.count_records_by_state(PublicationState.RIGHTS_DENIED) == 3
        assert await repo.count_records_by_state(PublicationState.PUBLISHED) == 0
        assert await repo.count_dry_run_plans() == 0
        assert await repo.count_work_identities() == 0


class TestStaleInFlightRecovery:
    """A crashed process must not wedge a record forever."""

    async def _publishing_record(self, repo, artwork_id="met:1", age_hours=3):
        record = PublicationRecord(artwork_canonical_id=artwork_id)
        record.transition_to(PublicationState.RESERVED, reason="reserved")
        record.transition_to(PublicationState.PUBLISHING, reason="write started")
        record.updated_at = datetime.now(UTC) - timedelta(hours=age_hours)
        await repo.save_record(record)
        return record

    async def test_abandoned_publishing_becomes_ambiguous(self, repo):
        """The write may have landed, so it must never be silently retried."""
        await self._publishing_record(repo)
        swept = await repo.sweep_stale_inflight_records(1800)

        assert swept == ["met:1"]
        record = await repo.get_record("met:1")
        assert record is not None
        assert record.state == PublicationState.AMBIGUOUS
        assert "Abandoned in publishing" in record.state_history[-1].reason
        # It is now visible to reconciliation.
        assert [
            r.artwork_canonical_id
            for r in await repo.get_records_by_state(PublicationState.AMBIGUOUS)
        ] == ["met:1"]

    async def test_abandoned_reserved_becomes_failed(self, repo):
        """RESERVED never sent a write, so retrying is safe."""
        from pinterest_art_bot.domain.publication import RETRYABLE_PUBLICATION_STATES

        record = PublicationRecord(artwork_canonical_id="met:2")
        record.transition_to(PublicationState.RESERVED, reason="reserved")
        record.updated_at = datetime.now(UTC) - timedelta(hours=3)
        await repo.save_record(record)

        await repo.sweep_stale_inflight_records(1800)
        recovered = await repo.get_record("met:2")
        assert recovered is not None
        assert recovered.state == PublicationState.FAILED
        assert recovered.state in RETRYABLE_PUBLICATION_STATES

    async def test_recent_in_flight_records_are_left_alone(self, repo):
        record = PublicationRecord(artwork_canonical_id="met:3")
        record.transition_to(PublicationState.RESERVED, reason="reserved")
        record.transition_to(PublicationState.PUBLISHING, reason="write started")
        record.updated_at = datetime.now(UTC)
        await repo.save_record(record)

        assert await repo.sweep_stale_inflight_records(1800) == []
        still = await repo.get_record("met:3")
        assert still is not None
        assert still.state == PublicationState.PUBLISHING

    async def test_ambiguous_and_published_are_never_swept(self, repo):
        from pinterest_art_bot.domain.publication import PublicationRecord as Record

        published = Record(artwork_canonical_id="met:4")
        published.transition_to(PublicationState.PUBLISHED)
        published.updated_at = datetime.now(UTC) - timedelta(days=10)
        await repo.save_record(published)

        ambiguous = Record(artwork_canonical_id="met:5")
        ambiguous.transition_to(PublicationState.RESERVED, reason="r")
        ambiguous.transition_to(PublicationState.PUBLISHING, reason="p")
        ambiguous.transition_to(PublicationState.AMBIGUOUS, reason="timeout")
        ambiguous.updated_at = datetime.now(UTC) - timedelta(days=10)
        await repo.save_record(ambiguous)

        assert await repo.sweep_stale_inflight_records(1800) == []

    async def test_sweep_is_disabled_with_zero_timeout(self, repo):
        await self._publishing_record(repo)
        assert await repo.sweep_stale_inflight_records(0) == []

    async def test_sweep_survives_restart(self, tmp_path):
        path = tmp_path / "state.db"
        first = SqliteStateRepository(path)
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.transition_to(PublicationState.RESERVED, reason="reserved")
        record.transition_to(PublicationState.PUBLISHING, reason="write started")
        record.updated_at = datetime.now(UTC) - timedelta(hours=5)
        await first.save_record(record)
        first.close()

        second = SqliteStateRepository(path)
        assert await second.sweep_stale_inflight_records(1800) == ["met:1"]
        recovered = await second.get_record("met:1")
        assert recovered is not None
        assert recovered.state == PublicationState.AMBIGUOUS
        second.close()

    async def test_in_memory_repository_sweep(self):
        from pinterest_art_bot.state.repository import InMemoryStateRepository

        memory = InMemoryStateRepository()
        record = PublicationRecord(artwork_canonical_id="met:1")
        record.transition_to(PublicationState.RESERVED, reason="r")
        record.transition_to(PublicationState.PUBLISHING, reason="p")
        record.updated_at = datetime.now(UTC) - timedelta(hours=5)
        await memory.save_record(record)

        assert await memory.sweep_stale_inflight_records(1800) == ["met:1"]
        assert (await memory.get_record("met:1")).state == PublicationState.AMBIGUOUS
