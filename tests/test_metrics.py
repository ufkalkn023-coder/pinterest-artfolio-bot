"""Tests for operational metrics derived from authoritative state."""

from datetime import UTC, datetime, timedelta

from pinterest_art_bot.domain.publication import (
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.observability.metrics import collect_metrics
from pinterest_art_bot.state.repository import InMemoryStateRepository


async def published_record(artwork_id: str, *, when: datetime | None = None) -> PublicationRecord:
    record = PublicationRecord(artwork_canonical_id=artwork_id)
    record.transition_to(PublicationState.PUBLISHED)
    if when is not None:
        record.published_at = when
    return record


async def ambiguous_record(artwork_id: str) -> PublicationRecord:
    record = PublicationRecord(artwork_canonical_id=artwork_id)
    record.transition_to(PublicationState.RESERVED, reason="r")
    record.transition_to(PublicationState.PUBLISHING, reason="p")
    record.transition_to(PublicationState.AMBIGUOUS, reason="timeout")
    return record


async def failed_record(artwork_id: str) -> PublicationRecord:
    record = PublicationRecord(artwork_canonical_id=artwork_id)
    record.transition_to(PublicationState.FAILED, reason="boom")
    return record


class TestCollection:
    async def test_empty_state(self):
        metrics = await collect_metrics(InMemoryStateRepository())
        assert metrics.published_24h == 0
        assert metrics.ambiguous_open == 0
        assert metrics.needs_attention is False
        assert metrics.warnings() == []

    async def test_counts_recent_publications(self):
        repo = InMemoryStateRepository()
        await repo.save_record(await published_record("met:1"))
        await repo.save_record(await published_record("met:2"))

        metrics = await collect_metrics(repo)
        assert metrics.published_24h == 2
        assert metrics.published_today == 2

    async def test_publications_outside_the_window_are_excluded(self):
        repo = InMemoryStateRepository()
        old = datetime.now(UTC) - timedelta(days=3)
        await repo.save_record(await published_record("met:1", when=old))

        metrics = await collect_metrics(repo, window_hours=24)
        assert metrics.published_24h == 0

    async def test_open_work_is_counted(self):
        repo = InMemoryStateRepository()
        await repo.save_record(await ambiguous_record("met:1"))
        await repo.save_record(await failed_record("met:2"))

        metrics = await collect_metrics(repo)
        assert metrics.ambiguous_open == 1
        assert metrics.failed_open == 1
        assert metrics.needs_attention is True

    async def test_dry_run_plans_and_known_works_are_counted(self):
        repo = InMemoryStateRepository()
        await repo.record_dry_run_plan("met:1", "run_1", {"artwork_id": "met:1"})

        from pinterest_art_bot.domain.work_identity import WorkIdentity

        identity = WorkIdentity(
            artist_tokens=frozenset({"degas"}), title_tokens=frozenset({"dance", "class"})
        )
        await repo.record_work_identity("k", "met:1", identity)

        metrics = await collect_metrics(repo)
        assert metrics.dry_run_plans == 1
        assert metrics.known_works == 1

    async def test_run_errors_are_summarised(self):
        repo = InMemoryStateRepository()
        now = datetime.now(UTC).isoformat()
        await repo.save_run(
            "run_1", {"museum": "met", "dry_run": True, "errors": 3, "finished_at": now}
        )
        await repo.save_run(
            "run_old",
            {
                "museum": "met",
                "dry_run": True,
                "errors": 9,
                "finished_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
            },
        )

        metrics = await collect_metrics(repo)
        assert metrics.runs_24h == 1
        assert metrics.errors_24h == 3

    async def test_last_run_breaks_down_rights_and_duplicates(self):
        repo = InMemoryStateRepository()
        last_run = {
            "rights_approved": 10,
            "rights_rejected": 2,
            "rights_review_required": 5,
            "duplicates_published": 1,
            "duplicates_cross_source": 3,
        }
        metrics = await collect_metrics(repo, last_run=last_run)
        assert metrics.rights.approved == 10
        assert metrics.rights.review_required == 5
        assert metrics.duplicates.cross_source == 3

    async def test_malformed_run_timestamps_are_ignored(self):
        repo = InMemoryStateRepository()
        await repo.save_run("run_x", {"museum": "met", "dry_run": True, "finished_at": "junk"})
        metrics = await collect_metrics(repo)
        assert metrics.runs_24h == 0


class TestAlerts:
    async def test_ambiguous_writes_raise_an_alert(self):
        repo = InMemoryStateRepository()
        await repo.save_record(await ambiguous_record("met:1"))
        warnings = (await collect_metrics(repo)).warnings()
        assert any("ambiguous" in warning.lower() for warning in warnings)

    async def test_errors_raise_an_alert(self):
        repo = InMemoryStateRepository()
        await repo.save_run(
            "run_1",
            {
                "museum": "met",
                "dry_run": False,
                "errors": 1,
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
        warnings = (await collect_metrics(repo)).warnings()
        assert any("processing errors" in warning for warning in warnings)

    async def test_policy_blocks_are_surfaced(self):
        repo = InMemoryStateRepository()
        warnings = (await collect_metrics(repo, last_run={"rights_review_required": 4})).warnings()
        assert any("rights policy" in warning for warning in warnings)

    async def test_clean_state_has_no_alerts(self):
        assert (await collect_metrics(InMemoryStateRepository())).warnings() == []
