"""SQLite-backed state repository.

This is the authoritative local/single-node implementation of StateRepository.
It is durable (survives process restarts), safe for concurrent async use
(synchronous sqlite3 calls are executed off the event loop and serialised behind
an asyncio lock), and crash-tolerant (WAL journaling).

ARCHITECTURAL NOTE:
  All pipeline and business logic depends on the StateRepository protocol, so
  this storage engine can be swapped for a distributed store later without
  touching the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import structlog

from pinterest_art_bot.domain.publication import (
    IN_FLIGHT_STATES,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.domain.work_identity import WorkIdentity

logger = structlog.get_logger()

SCHEMA_VERSION = 1

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    artwork_canonical_id TEXT PRIMARY KEY,
    run_id TEXT,
    state TEXT NOT NULL,
    pin_id TEXT,
    pin_url TEXT,
    image_sha256 TEXT,
    image_url_used TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    failed_at TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    record_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_hashes (
    image_sha256 TEXT PRIMARY KEY,
    artwork_canonical_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_perceptual_hashes (
    image_perceptual_hash TEXT NOT NULL,
    artwork_canonical_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (image_perceptual_hash, artwork_canonical_id)
);

CREATE INDEX IF NOT EXISTS idx_perceptual_artwork
    ON image_perceptual_hashes (artwork_canonical_id);

CREATE TABLE IF NOT EXISTS reservations (
    artwork_canonical_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    reserved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dry_run_plans (
    artwork_canonical_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    museum TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    counters_json TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_finished_at ON runs (finished_at DESC);

CREATE TABLE IF NOT EXISTS work_identities (
    work_key TEXT NOT NULL,
    artwork_canonical_id TEXT NOT NULL,
    artist_tokens TEXT NOT NULL,
    title_tokens TEXT NOT NULL,
    year INTEGER,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (work_key, artwork_canonical_id)
);

CREATE INDEX IF NOT EXISTS idx_work_identities_artwork
    ON work_identities (artwork_canonical_id);

CREATE TABLE IF NOT EXISTS cursors (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publications_state ON publications (state);
CREATE INDEX IF NOT EXISTS idx_publications_published_at ON publications (published_at);
"""


class SqliteStateRepository:
    """Durable SQLite implementation of StateRepository."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._in_memory = self._db_path == ":memory:"
        if not self._in_memory:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._configure_connection()
        self._init_schema()

    # ── Setup ────────────────────────────────────────────────────────
    def _configure_connection(self) -> None:
        """Apply durability and concurrency pragmas."""
        if not self._in_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _init_schema(self) -> None:
        """Create tables idempotently. Never destroys existing data."""
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # ── Public async API ─────────────────────────────────────────────
    async def get_record(self, artwork_id: str) -> PublicationRecord | None:
        return await self._run(self._get_record_sync, artwork_id)

    async def save_record(self, record: PublicationRecord) -> None:
        await self._run(self._save_record_sync, record)

    async def artwork_was_published(self, artwork_id: str) -> bool:
        return await self._run(self._artwork_was_published_sync, artwork_id)

    async def artwork_exists(self, artwork_id: str) -> bool:
        return await self._run(self._artwork_exists_sync, artwork_id)

    async def reserve_artwork(self, artwork_id: str, run_id: str) -> bool:
        return await self._run(self._reserve_artwork_sync, artwork_id, run_id)

    async def release_reservation(self, artwork_id: str, run_id: str) -> None:
        await self._run(self._release_reservation_sync, artwork_id, run_id)

    async def mark_ambiguous(self, artwork_id: str, error_message: str) -> None:
        record = await self.get_record(artwork_id)
        if record is None:
            logger.warning("mark_ambiguous_no_record", artwork_id=artwork_id)
            return
        record.transition_to(PublicationState.AMBIGUOUS, reason=error_message)
        record.error_message = error_message
        await self.save_record(record)

    async def get_records_by_state(self, state: PublicationState) -> list[PublicationRecord]:
        return await self._run(self._get_records_by_state_sync, state)

    async def check_image_hash(self, sha256: str) -> PublicationRecord | None:
        return await self._run(self._check_image_hash_sync, sha256)

    async def record_perceptual_hash(self, perceptual_hash: str, artwork_id: str) -> None:
        await self._run(self._record_perceptual_hash_sync, perceptual_hash, artwork_id)

    async def list_perceptual_hashes(self) -> list[tuple[str, str]]:
        return await self._run(self._list_perceptual_hashes_sync)

    async def record_work_identity(
        self,
        work_key: str,
        artwork_id: str,
        identity: WorkIdentity,
    ) -> None:
        await self._run(self._record_work_identity_sync, work_key, artwork_id, identity)

    async def list_work_identities(self) -> list[tuple[str, str, WorkIdentity]]:
        return await self._run(self._list_work_identities_sync)

    async def count_work_identities(self) -> int:
        return await self._run(self._count_work_identities_sync)

    async def count_published_since(self, since: datetime) -> int:
        return await self._run(self._count_published_since_sync, since)

    async def list_records(
        self,
        *,
        states: list[PublicationState] | None = None,
        limit: int = 50,
    ) -> list[PublicationRecord]:
        return await self._run(self._list_records_sync, states, limit)

    async def record_dry_run_plan(
        self, artwork_id: str, run_id: str, plan_data: dict[str, Any]
    ) -> None:
        await self._run(self._record_dry_run_plan_sync, artwork_id, run_id, plan_data)

    async def get_dry_run_plan(self, artwork_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_dry_run_plan_sync, artwork_id)

    async def list_dry_run_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._run(self._list_dry_run_plans_sync, limit)

    async def purge_expired_reservations(self, ttl_seconds: int) -> int:
        return await self._run(self._purge_expired_reservations_sync, ttl_seconds)

    async def purge_dry_run_plans(self, retention_seconds: int) -> int:
        return await self._run(self._purge_dry_run_plans_sync, retention_seconds)

    async def save_run(self, run_id: str, summary: dict[str, Any]) -> None:
        await self._run(self._save_run_sync, run_id, summary)

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._run(self._list_runs_sync, limit)

    async def count_published_per_board_since(self, since: datetime) -> dict[str, int]:
        return await self._run(self._count_published_per_board_since_sync, since)

    async def count_records_by_state(self, state: PublicationState) -> int:
        return await self._run(self._count_records_by_state_sync, state)

    async def count_dry_run_plans(self) -> int:
        return await self._run(self._count_dry_run_plans_sync)

    async def purge_run_history(self, retention_seconds: int) -> int:
        return await self._run(self._purge_run_history_sync, retention_seconds)

    async def sweep_stale_inflight_records(self, timeout_seconds: int) -> list[str]:
        return await self._run(self._sweep_stale_inflight_records_sync, timeout_seconds)

    async def purge_rejected_fingerprints(self, retention_seconds: int) -> int:
        return await self._run(self._purge_rejected_fingerprints_sync, retention_seconds)

    async def last_published_at_per_board(self) -> dict[str, datetime]:
        return await self._run(self._last_published_at_per_board_sync)

    async def get_cursor(self, name: str) -> int | None:
        return await self._run(self._get_cursor_sync, name)

    async def set_cursor(self, name: str, value: int) -> None:
        await self._run(self._set_cursor_sync, name, value)

    def close(self) -> None:
        """Close database connection."""
        self._conn.close()

    # ── Internals ────────────────────────────────────────────────────
    async def _run(self, func: Callable[..., T], *args: Any) -> T:
        """Execute a synchronous DB operation off the event loop, serialised."""
        async with self._lock:
            return await asyncio.to_thread(func, *args)

    # ── Synchronous implementations ─────────────────────────────────
    def _get_record_sync(self, artwork_id: str) -> PublicationRecord | None:
        row = self._conn.execute(
            "SELECT record_json FROM publications WHERE artwork_canonical_id = ?",
            (artwork_id,),
        ).fetchone()
        if row is None:
            return None
        return PublicationRecord.model_validate(json.loads(row["record_json"]))

    def _save_record_sync(self, record: PublicationRecord) -> None:
        now = datetime.now(UTC).isoformat()
        record_json = record.model_dump_json()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO publications (
                    artwork_canonical_id, run_id, state, pin_id, pin_url,
                    image_sha256, image_url_used, discovered_at, updated_at,
                    published_at, failed_at, error_message, retry_count, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artwork_canonical_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    state = excluded.state,
                    pin_id = excluded.pin_id,
                    pin_url = excluded.pin_url,
                    image_sha256 = excluded.image_sha256,
                    image_url_used = excluded.image_url_used,
                    updated_at = excluded.updated_at,
                    published_at = excluded.published_at,
                    failed_at = excluded.failed_at,
                    error_message = excluded.error_message,
                    retry_count = excluded.retry_count,
                    record_json = excluded.record_json
                """,
                (
                    record.artwork_canonical_id,
                    record.run_id,
                    record.state.value,
                    record.pin_id,
                    record.pin_url,
                    record.image_sha256,
                    record.image_url_used,
                    record.discovered_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.published_at.isoformat() if record.published_at else None,
                    record.failed_at.isoformat() if record.failed_at else None,
                    record.error_message,
                    record.retry_count,
                    record_json,
                ),
            )
            if record.image_sha256:
                self._conn.execute(
                    """
                    INSERT INTO image_hashes (image_sha256, artwork_canonical_id, recorded_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(image_sha256) DO UPDATE SET
                        artwork_canonical_id = excluded.artwork_canonical_id
                    """,
                    (
                        record.image_sha256,
                        record.artwork_canonical_id,
                        now,
                    ),
                )
            if record.image_perceptual_hash:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO image_perceptual_hashes
                        (image_perceptual_hash, artwork_canonical_id, recorded_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        record.image_perceptual_hash,
                        record.artwork_canonical_id,
                        now,
                    ),
                )

    def _artwork_was_published_sync(self, artwork_id: str) -> bool:
        row = self._conn.execute(
            "SELECT state FROM publications WHERE artwork_canonical_id = ?",
            (artwork_id,),
        ).fetchone()
        return row is not None and row["state"] == PublicationState.PUBLISHED.value

    def _artwork_exists_sync(self, artwork_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM publications WHERE artwork_canonical_id = ?",
            (artwork_id,),
        ).fetchone()
        return row is not None

    def _reserve_artwork_sync(self, artwork_id: str, run_id: str) -> bool:
        # INVARIANT: published or ambiguous artworks are never reservable.
        row = self._conn.execute(
            "SELECT state FROM publications WHERE artwork_canonical_id = ?",
            (artwork_id,),
        ).fetchone()
        if row and row["state"] in (
            PublicationState.PUBLISHED.value,
            PublicationState.AMBIGUOUS.value,
        ):
            return False

        existing = self._conn.execute(
            "SELECT run_id FROM reservations WHERE artwork_canonical_id = ?",
            (artwork_id,),
        ).fetchone()
        if existing:
            return bool(existing["run_id"] == run_id)

        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO reservations "
                    "(artwork_canonical_id, run_id, reserved_at) VALUES (?, ?, ?)",
                    (artwork_id, run_id, datetime.now(UTC).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def _release_reservation_sync(self, artwork_id: str, run_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM reservations WHERE artwork_canonical_id = ? AND run_id = ?",
                (artwork_id, run_id),
            )

    def _get_records_by_state_sync(self, state: PublicationState) -> list[PublicationRecord]:
        rows = self._conn.execute(
            "SELECT record_json FROM publications WHERE state = ? ORDER BY updated_at DESC",
            (state.value,),
        ).fetchall()
        return [PublicationRecord.model_validate(json.loads(row["record_json"])) for row in rows]

    def _check_image_hash_sync(self, sha256: str) -> PublicationRecord | None:
        row = self._conn.execute(
            "SELECT artwork_canonical_id FROM image_hashes WHERE image_sha256 = ?",
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return self._get_record_sync(row["artwork_canonical_id"])

    def _record_perceptual_hash_sync(self, perceptual_hash: str, artwork_id: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO image_perceptual_hashes
                    (image_perceptual_hash, artwork_canonical_id, recorded_at)
                VALUES (?, ?, ?)
                """,
                (perceptual_hash, artwork_id, datetime.now(UTC).isoformat()),
            )

    def _list_perceptual_hashes_sync(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT image_perceptual_hash, artwork_canonical_id FROM image_perceptual_hashes"
        ).fetchall()
        return [(row["image_perceptual_hash"], row["artwork_canonical_id"]) for row in rows]

    def _count_work_identities_sync(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM work_identities").fetchone()
        return int(row["n"]) if row else 0

    def _record_work_identity_sync(
        self,
        work_key: str,
        artwork_id: str,
        identity: WorkIdentity,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO work_identities
                    (work_key, artwork_canonical_id, artist_tokens, title_tokens,
                     year, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    work_key,
                    artwork_id,
                    ",".join(sorted(identity.artist_tokens)),
                    ",".join(sorted(identity.title_tokens)),
                    identity.year,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def _list_work_identities_sync(self) -> list[tuple[str, str, WorkIdentity]]:
        rows = self._conn.execute(
            "SELECT work_key, artwork_canonical_id, artist_tokens, title_tokens, year "
            "FROM work_identities"
        ).fetchall()
        identities: list[tuple[str, str, WorkIdentity]] = []
        for row in rows:
            identity = WorkIdentity(
                artist_tokens=frozenset(filter(None, row["artist_tokens"].split(","))),
                title_tokens=frozenset(filter(None, row["title_tokens"].split(","))),
                year=int(row["year"]) if row["year"] is not None else None,
            )
            identities.append((row["work_key"], row["artwork_canonical_id"], identity))
        return identities

    def _count_published_since_sync(self, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM publications "
            "WHERE state = ? AND published_at IS NOT NULL AND published_at >= ?",
            (PublicationState.PUBLISHED.value, since.isoformat()),
        ).fetchone()
        return int(row["n"]) if row else 0

    def _count_records_by_state_sync(self, state: PublicationState) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM publications WHERE state = ?", (state.value,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def _count_dry_run_plans_sync(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM dry_run_plans").fetchone()
        return int(row["n"]) if row else 0

    def _sweep_stale_inflight_records_sync(self, timeout_seconds: int) -> list[str]:
        """Recover records abandoned mid-write by a crashed process.

        PUBLISHING means a write may already have reached Pinterest, so it becomes
        AMBIGUOUS and must be reconciled. RESERVED never sent anything, so it
        becomes FAILED and may be retried.
        """
        if timeout_seconds <= 0:
            return []
        cutoff = (datetime.now(UTC) - timedelta(seconds=timeout_seconds)).isoformat()
        placeholders = ",".join("?" for _ in IN_FLIGHT_STATES)
        rows = self._conn.execute(
            f"SELECT record_json FROM publications "
            f"WHERE state IN ({placeholders}) AND updated_at < ?",
            (*(state.value for state in IN_FLIGHT_STATES), cutoff),
        ).fetchall()

        swept: list[str] = []
        with self._conn:
            for row in rows:
                record = PublicationRecord.model_validate(json.loads(row["record_json"]))
                target = (
                    PublicationState.AMBIGUOUS
                    if record.state == PublicationState.PUBLISHING
                    else PublicationState.FAILED
                )
                record.transition_to(
                    target,
                    reason=f"Abandoned in {record.state.value} for over {timeout_seconds}s",
                )
                self._conn.execute(
                    """
                    UPDATE publications
                    SET state = ?, updated_at = ?, record_json = ?
                    WHERE artwork_canonical_id = ?
                    """,
                    (
                        record.state.value,
                        datetime.now(UTC).isoformat(),
                        record.model_dump_json(),
                        record.artwork_canonical_id,
                    ),
                )
                swept.append(record.artwork_canonical_id)
        return swept

    def _purge_run_history_sync(self, retention_seconds: int) -> int:
        if retention_seconds <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention_seconds)).isoformat()
        with self._conn:
            cursor = self._conn.execute("DELETE FROM runs WHERE finished_at < ?", (cutoff,))
        return max(0, cursor.rowcount)

    def _purge_rejected_fingerprints_sync(self, retention_seconds: int) -> int:
        """Drop fingerprints of artworks that can never be published.

        Anything that could still publish — including FAILED, which stays
        retryable — keeps its fingerprints, because a lost fingerprint would let
        the same image or work be published a second time.
        """
        if retention_seconds <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention_seconds)).isoformat()
        rejected = (PublicationState.RIGHTS_DENIED.value, PublicationState.REJECTED.value)
        placeholders = ",".join("?" for _ in rejected)
        removed = 0
        with self._conn:
            cursor = self._conn.execute(
                f"SELECT artwork_canonical_id FROM publications "
                f"WHERE state IN ({placeholders}) AND discovered_at < ?",
                (*rejected, cutoff),
            )
            stale = [row["artwork_canonical_id"] for row in cursor.fetchall()]
            for artwork_id in stale:
                removed += self._conn.execute(
                    "DELETE FROM image_hashes WHERE artwork_canonical_id = ?", (artwork_id,)
                ).rowcount
                removed += self._conn.execute(
                    "DELETE FROM image_perceptual_hashes WHERE artwork_canonical_id = ?",
                    (artwork_id,),
                ).rowcount
                removed += self._conn.execute(
                    "DELETE FROM work_identities WHERE artwork_canonical_id = ?", (artwork_id,)
                ).rowcount
        return max(0, removed)

    def _count_published_per_board_since_sync(self, since: datetime) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT record_json FROM publications "
            "WHERE state = ? AND published_at IS NOT NULL AND published_at >= ?",
            (PublicationState.PUBLISHED.value, since.isoformat()),
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            record = PublicationRecord.model_validate(json.loads(row["record_json"]))
            if record.board_target is None:
                continue
            key = record.board_target.board_key
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _last_published_at_per_board_sync(self) -> dict[str, datetime]:
        rows = self._conn.execute(
            "SELECT record_json FROM publications WHERE state = ? AND published_at IS NOT NULL",
            (PublicationState.PUBLISHED.value,),
        ).fetchall()
        latest: dict[str, datetime] = {}
        for row in rows:
            record = PublicationRecord.model_validate(json.loads(row["record_json"]))
            if record.board_target is None or record.published_at is None:
                continue
            key = record.board_target.board_key
            if key not in latest or record.published_at > latest[key]:
                latest[key] = record.published_at
        return latest

    def _list_records_sync(
        self,
        states: list[PublicationState] | None,
        limit: int,
    ) -> list[PublicationRecord]:
        if states:
            placeholders = ",".join("?" for _ in states)
            rows = self._conn.execute(
                f"SELECT record_json FROM publications "
                f"WHERE state IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                (*[state.value for state in states], limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT record_json FROM publications ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [PublicationRecord.model_validate(json.loads(row["record_json"])) for row in rows]

    def _record_dry_run_plan_sync(
        self, artwork_id: str, run_id: str, plan_data: dict[str, Any]
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO dry_run_plans (artwork_canonical_id, run_id, plan_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(artwork_canonical_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    plan_json = excluded.plan_json,
                    created_at = excluded.created_at
                """,
                (
                    artwork_id,
                    run_id,
                    json.dumps(plan_data),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def _get_dry_run_plan_sync(self, artwork_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT plan_json FROM dry_run_plans WHERE artwork_canonical_id = ?",
            (artwork_id,),
        ).fetchone()
        if row:
            plan: dict[str, Any] = json.loads(row["plan_json"])
            return plan
        return None

    def _purge_expired_reservations_sync(self, ttl_seconds: int) -> int:
        if ttl_seconds <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=ttl_seconds)).isoformat()
        with self._conn:
            cursor = self._conn.execute("DELETE FROM reservations WHERE reserved_at < ?", (cutoff,))
        return cursor.rowcount if cursor.rowcount > 0 else 0

    def _list_dry_run_plans_sync(self, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT plan_json FROM dry_run_plans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        plans: list[dict[str, Any]] = []
        for row in rows:
            plan: dict[str, Any] = json.loads(row["plan_json"])
            plans.append(plan)
        return plans

    def _save_run_sync(self, run_id: str, summary: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        counters = {
            key: value
            for key, value in summary.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (run_id, museum, dry_run, started_at, finished_at,
                                  counters_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    museum = excluded.museum,
                    dry_run = excluded.dry_run,
                    finished_at = excluded.finished_at,
                    counters_json = excluded.counters_json,
                    error = excluded.error
                """,
                (
                    run_id,
                    str(summary.get("museum", "")),
                    1 if summary.get("dry_run", True) else 0,
                    str(summary.get("started_at", now)),
                    str(summary.get("finished_at", now)),
                    json.dumps(counters),
                    summary.get("error"),
                ),
            )

    def _list_runs_sync(self, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, museum, dry_run, started_at, finished_at, counters_json, error "
            "FROM runs ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        runs: list[dict[str, Any]] = []
        for row in rows:
            counters: dict[str, Any] = json.loads(row["counters_json"])
            runs.append(
                {
                    "run_id": row["run_id"],
                    "museum": row["museum"],
                    "dry_run": bool(row["dry_run"]),
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "error": row["error"],
                    **counters,
                }
            )
        return runs

    def _get_cursor_sync(self, name: str) -> int | None:
        row = self._conn.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return int(row["value"]) if row else None

    def _set_cursor_sync(self, name: str, value: int) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO cursors (name, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (name, value, datetime.now(UTC).isoformat()),
            )

    def _purge_dry_run_plans_sync(self, retention_seconds: int) -> int:
        if retention_seconds <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=retention_seconds)).isoformat()
        with self._conn:
            cursor = self._conn.execute("DELETE FROM dry_run_plans WHERE created_at < ?", (cutoff,))
        return cursor.rowcount if cursor.rowcount > 0 else 0
