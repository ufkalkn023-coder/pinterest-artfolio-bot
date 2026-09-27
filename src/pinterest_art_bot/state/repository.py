"""State repository — storage abstraction for publication state.

This Protocol defines the contract for durable state management.
Implementations may use in-memory storage (dev/test), SQLite (local + single-node
production), or a future distributed store (PostgreSQL, D1/R2, DynamoDB).

ARCHITECTURAL NOTE:
  The pipeline depends on this protocol only. Storage choices never leak into
  business logic. The repository is authoritative for:
    - publication history (deduplication),
    - write reservations (concurrency),
    - ambiguous write bookkeeping (reconciliation),
    - media fingerprints (exact + near-duplicate detection).

INVARIANT:
  `artwork_was_published()` must never report False for an artwork that is
  already published. Deduplication is a hard safety property.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pinterest_art_bot.domain.publication import (
    IN_FLIGHT_STATES,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.domain.work_identity import WorkIdentity


@runtime_checkable
class StateRepository(Protocol):
    """Protocol for publication state persistence."""

    async def get_record(self, artwork_id: str) -> PublicationRecord | None:
        """Look up a publication record by canonical artwork ID."""
        ...

    async def save_record(self, record: PublicationRecord) -> None:
        """Save or update a publication record."""
        ...

    async def artwork_was_published(self, artwork_id: str) -> bool:
        """Check if an artwork has already been successfully published."""
        ...

    async def artwork_exists(self, artwork_id: str) -> bool:
        """Check if an artwork has any record (any state)."""
        ...

    async def reserve_artwork(self, artwork_id: str, run_id: str) -> bool:
        """Attempt to reserve an artwork for a pipeline run.

        Returns True if the reservation was granted (no other run holds it).
        Returns False if the artwork is already reserved or published.
        This is the primary concurrency guard.
        """
        ...

    async def release_reservation(self, artwork_id: str, run_id: str) -> None:
        """Release a reservation, allowing the artwork to be processed again."""
        ...

    async def mark_ambiguous(self, artwork_id: str, error_message: str) -> None:
        """Mark an artwork as having an ambiguous write result.

        This artwork must NOT be retried without reconciliation.
        """
        ...

    async def get_records_by_state(self, state: PublicationState) -> list[PublicationRecord]:
        """Retrieve all records in a given state."""
        ...

    async def check_image_hash(self, sha256: str) -> PublicationRecord | None:
        """Look up a record by image SHA-256 hash for dedup."""
        ...

    async def record_perceptual_hash(self, perceptual_hash: str, artwork_id: str) -> None:
        """Store a perceptual hash for near-duplicate detection."""
        ...

    async def list_perceptual_hashes(self) -> list[tuple[str, str]]:
        """Return all (perceptual_hash, artwork_id) pairs for near-duplicate matching."""
        ...

    async def record_work_identity(
        self,
        work_key: str,
        artwork_id: str,
        identity: WorkIdentity,
    ) -> None:
        """Store a cross-source work identity for duplicate detection."""
        ...

    async def list_work_identities(self) -> list[tuple[str, str, WorkIdentity]]:
        """Return all (work_key, artwork_id, identity) triples."""
        ...

    async def count_work_identities(self) -> int:
        """Count stored cross-source work identities without loading them."""
        ...

    async def count_published_since(self, since: datetime) -> int:
        """Count records published at or after the given instant."""
        ...

    async def count_published_per_board_since(self, since: datetime) -> dict[str, int]:
        """Count records published per board key since the given instant."""
        ...

    async def count_records_by_state(self, state: PublicationState) -> int:
        """Count records in a state without loading them all."""
        ...

    async def count_dry_run_plans(self) -> int:
        """Count stored dry-run plans without loading them."""
        ...

    async def purge_run_history(self, retention_seconds: int) -> int:
        """Delete run history older than the retention window."""
        ...

    async def sweep_stale_inflight_records(self, timeout_seconds: int) -> list[str]:
        """Recover records abandoned mid-write by a crashed process.

        A record left in PUBLISHING means a write may or may not have reached
        Pinterest, so it becomes AMBIGUOUS and must be reconciled. A record left
        in RESERVED never started a write, so it becomes FAILED and may be
        retried. Returns the affected artwork IDs.
        """
        ...

    async def purge_rejected_fingerprints(self, retention_seconds: int) -> int:
        """Delete media fingerprints belonging to permanently rejected artworks.

        Only artworks that can never publish (rights-denied, rejected) are
        eligible: dropping a fingerprint for a live or published work could
        allow it to be published twice.
        """
        ...

    async def last_published_at_per_board(self) -> dict[str, datetime]:
        """Return the most recent publication time per board key."""
        ...

    async def list_records(
        self,
        *,
        states: list[PublicationState] | None = None,
        limit: int = 50,
    ) -> list[PublicationRecord]:
        """List records, optionally filtered by state, most recently updated first."""
        ...

    async def record_dry_run_plan(
        self, artwork_id: str, run_id: str, plan_data: dict[str, Any]
    ) -> None:
        """Store a dry-run plan in separate storage without affecting publication state."""
        ...

    async def get_dry_run_plan(self, artwork_id: str) -> dict[str, Any] | None:
        """Retrieve a stored dry-run plan."""
        ...

    async def list_dry_run_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        """List stored dry-run plans, most recent first."""
        ...

    async def purge_expired_reservations(self, ttl_seconds: int) -> int:
        """Delete reservations older than the TTL. Returns the number removed."""
        ...

    async def purge_dry_run_plans(self, retention_seconds: int) -> int:
        """Delete dry-run plans older than the retention window. Returns the number removed."""
        ...

    async def save_run(self, run_id: str, summary: dict[str, Any]) -> None:
        """Persist a completed pipeline run for auditing and reporting."""
        ...

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent run summaries, most recent first."""
        ...

    async def get_cursor(self, name: str) -> int | None:
        """Read a persisted discovery cursor (e.g. how far a museum scan has progressed)."""
        ...

    async def set_cursor(self, name: str, value: int) -> None:
        """Persist a discovery cursor so successive runs explore new ground."""
        ...


class InMemoryStateRepository:
    """In-memory state repository for development and testing.

    NOT suitable for production — state is lost on process exit.
    """

    def __init__(self) -> None:
        self._records: dict[str, PublicationRecord] = {}
        self._reservations: dict[str, str] = {}  # artwork_id → run_id
        self._image_hashes: dict[str, str] = {}  # sha256 → artwork_id
        self._perceptual_hashes: dict[str, str] = {}  # perceptual_hash → artwork_id
        self._dry_run_plans: dict[str, dict[str, Any]] = {}
        self._cursors: dict[str, int] = {}
        self._work_identities: dict[str, tuple[str, WorkIdentity]] = {}  # key → (id, identity)
        self._runs: dict[str, dict[str, Any]] = {}  # run_id → summary

    async def get_record(self, artwork_id: str) -> PublicationRecord | None:
        return self._records.get(artwork_id)

    async def save_record(self, record: PublicationRecord) -> None:
        self._records[record.artwork_canonical_id] = record
        if record.image_sha256:
            self._image_hashes[record.image_sha256] = record.artwork_canonical_id
        if record.image_perceptual_hash:
            self._perceptual_hashes[record.image_perceptual_hash] = record.artwork_canonical_id

    async def artwork_was_published(self, artwork_id: str) -> bool:
        record = self._records.get(artwork_id)
        return record is not None and record.state == PublicationState.PUBLISHED

    async def artwork_exists(self, artwork_id: str) -> bool:
        return artwork_id in self._records

    async def reserve_artwork(self, artwork_id: str, run_id: str) -> bool:
        if artwork_id in self._reservations:
            return self._reservations[artwork_id] == run_id
        record = self._records.get(artwork_id)
        if record and record.state in (PublicationState.PUBLISHED, PublicationState.AMBIGUOUS):
            return False
        self._reservations[artwork_id] = run_id
        return True

    async def release_reservation(self, artwork_id: str, run_id: str) -> None:
        if self._reservations.get(artwork_id) == run_id:
            del self._reservations[artwork_id]

    async def mark_ambiguous(self, artwork_id: str, error_message: str) -> None:
        record = self._records.get(artwork_id)
        if record:
            record.transition_to(PublicationState.AMBIGUOUS, reason=error_message)
            record.error_message = error_message

    async def get_records_by_state(self, state: PublicationState) -> list[PublicationRecord]:
        return [r for r in self._records.values() if r.state == state]

    async def check_image_hash(self, sha256: str) -> PublicationRecord | None:
        artwork_id = self._image_hashes.get(sha256)
        if artwork_id:
            return self._records.get(artwork_id)
        return None

    async def record_perceptual_hash(self, perceptual_hash: str, artwork_id: str) -> None:
        self._perceptual_hashes.setdefault(perceptual_hash, artwork_id)

    async def list_perceptual_hashes(self) -> list[tuple[str, str]]:
        return list(self._perceptual_hashes.items())

    async def record_work_identity(
        self,
        work_key: str,
        artwork_id: str,
        identity: WorkIdentity,
    ) -> None:
        self._work_identities.setdefault(work_key, (artwork_id, identity))

    async def count_work_identities(self) -> int:
        return len(self._work_identities)

    async def list_work_identities(self) -> list[tuple[str, str, WorkIdentity]]:
        return [
            (key, artwork_id, identity)
            for key, (artwork_id, identity) in self._work_identities.items()
        ]

    async def count_published_since(self, since: datetime) -> int:
        return sum(
            1
            for record in self._records.values()
            if record.state == PublicationState.PUBLISHED
            and record.published_at is not None
            and record.published_at >= since
        )

    async def count_published_per_board_since(self, since: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records.values():
            if record.state != PublicationState.PUBLISHED or record.published_at is None:
                continue
            if record.published_at < since:
                continue
            if record.board_target is None:
                continue
            key = record.board_target.board_key
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def last_published_at_per_board(self) -> dict[str, datetime]:
        latest: dict[str, datetime] = {}
        for record in self._records.values():
            if record.state != PublicationState.PUBLISHED or record.published_at is None:
                continue
            if record.board_target is None:
                continue
            key = record.board_target.board_key
            if key not in latest or record.published_at > latest[key]:
                latest[key] = record.published_at
        return latest

    async def list_records(
        self,
        *,
        states: list[PublicationState] | None = None,
        limit: int = 50,
    ) -> list[PublicationRecord]:
        records = list(self._records.values())
        if states:
            records = [r for r in records if r.state in states]
        return records[:limit]

    async def record_dry_run_plan(
        self, artwork_id: str, run_id: str, plan_data: dict[str, Any]
    ) -> None:
        self._dry_run_plans[artwork_id] = plan_data

    async def get_dry_run_plan(self, artwork_id: str) -> dict[str, Any] | None:
        return self._dry_run_plans.get(artwork_id)

    async def list_dry_run_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        plans = list(self._dry_run_plans.values())
        plans.reverse()
        return plans[:limit]

    async def purge_expired_reservations(self, ttl_seconds: int) -> int:
        return 0

    async def purge_dry_run_plans(self, retention_seconds: int) -> int:
        return 0

    async def count_records_by_state(self, state: PublicationState) -> int:
        return sum(1 for r in self._records.values() if r.state == state)

    async def count_dry_run_plans(self) -> int:
        return len(self._dry_run_plans)

    async def purge_run_history(self, retention_seconds: int) -> int:
        return 0

    async def sweep_stale_inflight_records(self, timeout_seconds: int) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
        swept: list[str] = []
        for record in list(self._records.values()):
            if record.state not in IN_FLIGHT_STATES or record.updated_at >= cutoff:
                continue
            target = (
                PublicationState.AMBIGUOUS
                if record.state == PublicationState.PUBLISHING
                else PublicationState.FAILED
            )
            record.transition_to(
                target, reason=f"Abandoned in {record.state.value} for over {timeout_seconds}s"
            )
            swept.append(record.artwork_canonical_id)
        return swept

    async def purge_rejected_fingerprints(self, retention_seconds: int) -> int:
        return 0

    async def save_run(self, run_id: str, summary: dict[str, Any]) -> None:
        self._runs[run_id] = {"run_id": run_id, **summary}

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        runs = list(self._runs.values())
        runs.reverse()
        return runs[:limit]

    async def get_cursor(self, name: str) -> int | None:
        return self._cursors.get(name)

    async def set_cursor(self, name: str, value: int) -> None:
        self._cursors[name] = value

    def close(self) -> None:
        """No-op: nothing to release."""
