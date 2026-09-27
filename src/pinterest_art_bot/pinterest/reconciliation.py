"""Ambiguous write reconciliation.

When a Pinterest write may or may not have landed, the record is parked in the
AMBIGUOUS state. This module is the ONLY way out of that state.

INVARIANTS:
  - An ambiguous write is never retried from here. We look for evidence instead.
  - A record leaves AMBIGUOUS only as PUBLISHED (the Pin exists) or RECONCILED
    (we definitively determined no Pin exists).
  - Without a known board and a known source link, we cannot prove anything, so
    the record stays AMBIGUOUS for manual review.
"""

from __future__ import annotations

from enum import StrEnum

import structlog
from pydantic import BaseModel, Field

from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState
from pinterest_art_bot.pinterest.client import PinterestClient
from pinterest_art_bot.pinterest.models import PinterestPin
from pinterest_art_bot.state.repository import StateRepository

logger = structlog.get_logger()

# Must match the client's page size, used only to detect a truncated scan.
_BOARD_PAGE_SIZE = 100


class ReconciliationOutcome(StrEnum):
    """Terminal outcome of a reconciliation attempt."""

    PUBLISHED = "published"
    RECONCILED = "reconciled"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    SKIPPED = "skipped"


class ReconciliationResult(BaseModel):
    """Outcome for a single record."""

    artwork_id: str
    outcome: ReconciliationOutcome
    reason: str
    pin_id: str | None = None
    pin_url: str | None = None
    state_after: PublicationState = PublicationState.AMBIGUOUS


class ReconciliationSummary(BaseModel):
    """Aggregate outcome of a reconciliation pass."""

    examined: int = 0
    published: int = 0
    reconciled: int = 0
    manual_review_required: int = 0
    remaining: int = 0
    results: list[ReconciliationResult] = Field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        """Whether work was deliberately left for a later pass."""
        return self.remaining > 0


class AmbiguousWriteReconciler:
    """Resolves AMBIGUOUS records by inspecting Pinterest for the actual outcome."""

    def __init__(
        self,
        client: PinterestClient,
        state_repo: StateRepository,
        *,
        max_records: int = 20,
        max_pages_per_board: int = 10,
    ) -> None:
        self._client = client
        self._state_repo = state_repo
        self._max_records = max(1, max_records)
        self._max_pages_per_board = max(1, max_pages_per_board)

    async def reconcile_all(self) -> ReconciliationSummary:
        """Reconcile AMBIGUOUS records, within an explicit work budget.

        Both the number of records and the depth of each board scan are bounded,
        so a backlog cannot turn one call into hours of API traffic. Anything
        left over is reported as ``remaining`` for a later pass.
        """
        pending = await self._state_repo.get_records_by_state(PublicationState.AMBIGUOUS)
        records = pending[: self._max_records]
        summary = ReconciliationSummary(
            examined=len(records),
            remaining=max(0, len(pending) - len(records)),
        )

        for record in records:
            result = await self.reconcile_record(record)
            summary.results.append(result)
            if result.outcome == ReconciliationOutcome.PUBLISHED:
                summary.published += 1
            elif result.outcome == ReconciliationOutcome.RECONCILED:
                summary.reconciled += 1
            elif result.outcome == ReconciliationOutcome.MANUAL_REVIEW_REQUIRED:
                summary.manual_review_required += 1

        logger.info(
            "reconciliation_completed",
            examined=summary.examined,
            published=summary.published,
            reconciled=summary.reconciled,
            manual_review_required=summary.manual_review_required,
            remaining=summary.remaining,
        )
        return summary

    async def reconcile_record(self, record: PublicationRecord) -> ReconciliationResult:
        """Attempt to definitively resolve one ambiguous record."""
        if record.state != PublicationState.AMBIGUOUS:
            return ReconciliationResult(
                artwork_id=record.artwork_canonical_id,
                outcome=ReconciliationOutcome.SKIPPED,
                reason=f"Record is in {record.state.value}, not AMBIGUOUS.",
                state_after=record.state,
            )

        if record.pin_id:
            return await self._reconcile_known_pin(record)

        if not self._has_board_and_link(record):
            return await self._require_manual_review(
                record,
                "Record has neither a known Pin ID nor a board+link pair to scan. "
                "Cannot prove the write outcome automatically.",
            )

        return await self._scan_board_for_pin(record)

    # ── Strategies ──────────────────────────────────────────────────
    async def _reconcile_known_pin(self, record: PublicationRecord) -> ReconciliationResult:
        """We know the Pin ID: either the Pin exists or it definitively does not."""
        pin = await self._client.get_pin(record.pin_id or "")
        if pin is not None:
            return await self._mark_published(record, pin)

        logger.info(
            "reconciliation_pin_absent",
            artwork_id=record.artwork_canonical_id,
            pin_id=record.pin_id,
        )
        return await self._mark_reconciled(
            record,
            reason=f"Pin {record.pin_id} does not exist on Pinterest. Write did not persist.",
        )

    async def _scan_board_for_pin(self, record: PublicationRecord) -> ReconciliationResult:
        """Scan the target board for a Pin matching our source link or media URL."""
        board_target = record.board_target
        assert board_target is not None  # guarded by _has_board_and_link
        board_id = board_target.board_id or ""

        pins = await self._client.list_board_pins(board_id, max_pages=self._max_pages_per_board)

        for pin in pins:
            if _pin_matches(record, pin):
                return await self._mark_published(record, pin)

        # Only a complete scan proves absence. A capped scan may have missed the
        # Pin, so the record stays AMBIGUOUS instead of being wrongly resolved.
        if len(pins) >= self._max_pages_per_board * _BOARD_PAGE_SIZE:
            return await self._require_manual_review(
                record,
                f"Board {board_id} has more Pins than the scan budget "
                f"({self._max_pages_per_board} pages). Raise the limit to reconcile this record.",
            )

        return await self._mark_reconciled(
            record,
            reason=(
                f"Scanned {len(pins)} Pin(s) on board {board_id}; no Pin matches the "
                "recorded source link. Write did not persist."
            ),
        )

    async def _require_manual_review(
        self,
        record: PublicationRecord,
        reason: str,
    ) -> ReconciliationResult:
        logger.warning(
            "reconciliation_manual_review_required",
            artwork_id=record.artwork_canonical_id,
            reason=reason,
        )
        return ReconciliationResult(
            artwork_id=record.artwork_canonical_id,
            outcome=ReconciliationOutcome.MANUAL_REVIEW_REQUIRED,
            reason=reason,
            state_after=PublicationState.AMBIGUOUS,
        )

    # ── State transitions ───────────────────────────────────────────
    async def _mark_published(
        self,
        record: PublicationRecord,
        pin: PinterestPin,
    ) -> ReconciliationResult:
        record.pin_id = pin.id
        record.transition_to(
            PublicationState.PUBLISHED,
            reason=f"Reconciled: Pin {pin.id} confirmed on Pinterest.",
        )
        await self._state_repo.save_record(record)
        logger.info(
            "reconciliation_resolved_published",
            artwork_id=record.artwork_canonical_id,
            pin_id=pin.id,
        )
        return ReconciliationResult(
            artwork_id=record.artwork_canonical_id,
            outcome=ReconciliationOutcome.PUBLISHED,
            reason=f"Pin {pin.id} exists on Pinterest.",
            pin_id=pin.id,
            state_after=PublicationState.PUBLISHED,
        )

    async def _mark_reconciled(
        self,
        record: PublicationRecord,
        *,
        reason: str,
    ) -> ReconciliationResult:
        record.transition_to(PublicationState.RECONCILED, reason=reason)
        await self._state_repo.save_record(record)
        logger.info(
            "reconciliation_resolved_not_published",
            artwork_id=record.artwork_canonical_id,
        )
        return ReconciliationResult(
            artwork_id=record.artwork_canonical_id,
            outcome=ReconciliationOutcome.RECONCILED,
            reason=reason,
            state_after=PublicationState.RECONCILED,
        )

    @staticmethod
    def _has_board_and_link(record: PublicationRecord) -> bool:
        board_target = record.board_target
        has_identity = bool(record.link) or bool(record.image_url_used)
        return bool(board_target and board_target.board_id and has_identity)


def _pin_matches(record: PublicationRecord, pin: PinterestPin) -> bool:
    """Whether a remote Pin corresponds to our attempted write."""
    if _same_value(record.link, pin.link):
        return True
    return _same_value(record.image_url_used, pin.media_url)


def _same_value(expected: str | None, actual: str | None) -> bool:
    """Compare two optional URLs, ignoring surrounding whitespace."""
    if not expected or not actual:
        return False
    return expected.strip() == actual.strip()
