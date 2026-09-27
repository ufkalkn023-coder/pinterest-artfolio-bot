"""Single-artwork publication.

Owns the write half of the pipeline: board resolution, budget, reservation,
state transitions, and the single Pinterest call. Extracted from the runner so
the safety rules around one write are testable on their own.

INVARIANTS preserved by this module:
  - One attempt per call. Nothing here ever repeats a write.
  - A transport failure parks the record in AMBIGUOUS and is never retried here.
  - A definitive failure records the attempt and releases the reservation.
  - Budget is consumed before the write, so concurrency cannot overshoot it.
"""

from __future__ import annotations

import structlog

from pinterest_art_bot.boards.config import BoardsConfiguration
from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.movement import MovementClassification
from pinterest_art_bot.domain.publication import (
    BoardTarget,
    PinMetadata,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.domain.rights import RightsEvaluation
from pinterest_art_bot.media.preparation import ImagePreparer, MediaMode
from pinterest_art_bot.pinterest.boards_sync import normalize_board_name
from pinterest_art_bot.pinterest.client import PinterestClient
from pinterest_art_bot.pinterest.errors import (
    PinterestAmbiguousWriteError,
    PinterestError,
    ProductionWriteDisabledError,
)
from pinterest_art_bot.pinterest.models import CreatePinRequest
from pinterest_art_bot.pipeline.models import PipelineDecision, PipelineItemResult
from pinterest_art_bot.state.repository import StateRepository

logger = structlog.get_logger()


class PublicationService:
    """Writes one artwork to Pinterest with full state bookkeeping."""

    def __init__(
        self,
        *,
        client: PinterestClient,
        state_repo: StateRepository,
        boards_config: BoardsConfiguration,
        remote_board_ids: dict[str, str] | None = None,
        per_board_daily_limit: int = 0,
        preparer: ImagePreparer | None = None,
    ) -> None:
        self._client = client
        self._state_repo = state_repo
        self._boards_config = boards_config
        self._remote_board_ids = remote_board_ids or {}
        self._per_board_daily_limit = per_board_daily_limit
        self._preparer = preparer
        self._attempts_this_run = 0
        self._per_board_writes: dict[str, int] = {}
        # Board activity is loaded once per run so rotation stays cheap.
        self._board_activity: dict[str, int] = {}
        self._board_last_used: dict[str, str] = {}

    @property
    def attempts_this_run(self) -> int:
        """Writes attempted so far in this run, used for budget accounting."""
        return self._attempts_this_run

    def reset_attempts(self) -> None:
        """Start a fresh budget for a new run."""
        self._attempts_this_run = 0
        self._per_board_writes = {}

    def load_board_activity(
        self,
        published_per_board: dict[str, int],
        last_used_per_board: dict[str, str],
    ) -> None:
        """Provide today's per-board usage, for budgets and rotation."""
        self._board_activity = dict(published_per_board)
        self._board_last_used = dict(last_used_per_board)

    def board_rank(self, board_key: str) -> tuple[int, str, str]:
        """Sort key that spreads Pins across boards.

        Boards used least today come first; ties break on the least recently
        used, then alphabetically, so a run is deterministic but varied.
        """
        used_today = self._board_activity.get(board_key, 0) + self._per_board_writes.get(
            board_key, 0
        )
        return (used_today, self._board_last_used.get(board_key, ""), board_key)

    def board_budget_available(self, board_key: str) -> bool:
        """Whether this board still has room in today's allowance."""
        if self._per_board_daily_limit <= 0:
            return True
        used = self._board_activity.get(board_key, 0) + self._per_board_writes.get(board_key, 0)
        return used < self._per_board_daily_limit

    def set_remote_board_ids(self, board_ids: dict[str, str]) -> None:
        """Provide board IDs discovered from the live Pinterest account."""
        self._remote_board_ids = board_ids

    def resolve_board_id(self, target: BoardTarget) -> str | None:
        """Resolve a board ID from configuration first, then the live board list."""
        if target.board_id:
            return target.board_id

        board_config = self._boards_config.get_board_by_key(target.board_key)
        if board_config and board_config.board_id:
            return board_config.board_id

        return self._remote_board_ids.get(normalize_board_name(target.board_name))

    def consume_budget(self, budget: int) -> bool:
        """Whether one more write fits in this run's budget.

        Consumes the budget as a side effect, with no intervening await, so
        concurrent tasks cannot overshoot the cap.
        """
        if budget <= 0 or self._attempts_this_run >= budget:
            return False
        self._attempts_this_run += 1
        return True

    def note_board_write(self, board_key: str) -> None:
        """Count a write against a board's daily allowance."""
        self._per_board_writes[board_key] = self._per_board_writes.get(board_key, 0) + 1

    async def publish(
        self,
        *,
        artwork: Artwork,
        run_id: str,
        budget: int,
        rights_eval: RightsEvaluation,
        classification: MovementClassification,
        target_board: BoardTarget,
        pin_meta: PinMetadata,
        image_url: str,
        image_sha256: str,
        perceptual_hash: str | None,
        existing_record: PublicationRecord | None = None,
        image_bytes: bytes | None = None,
    ) -> PipelineItemResult:
        """Reserve, persist state, and write one Pin.

        Returns a PipelineItemResult describing the outcome. Counters live on the
        run summary, so this method never mutates them.
        """
        canonical_id = artwork.canonical_id
        movement_name = classification.primary_movement.value
        confidence = classification.confidence

        board_id = self.resolve_board_id(target_board)
        if not board_id:
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=artwork.title,
                decision=PipelineDecision.BOARD_ID_MISSING,
                reason=(
                    f"No Pinterest board ID resolved for '{target_board.board_name}'. "
                    "Run 'pinterest-art-bot boards-sync' first."
                ),
                movement=movement_name,
                confidence=confidence,
                board_key=target_board.board_key,
                image_sha256=image_sha256,
            )

        if not self.board_budget_available(target_board.board_key):
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=artwork.title,
                decision=PipelineDecision.BOARD_BUDGET_EXHAUSTED,
                reason=(
                    f"Board '{target_board.board_name}' has reached its daily Pin limit "
                    f"({self._per_board_daily_limit}). The artwork stays eligible for later runs."
                ),
                movement=movement_name,
                confidence=confidence,
                board_key=target_board.board_key,
                image_sha256=image_sha256,
            )

        if not self.consume_budget(budget):
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=artwork.title,
                decision=PipelineDecision.PIN_BUDGET_EXHAUSTED,
                reason=(
                    "Pin budget for this run is exhausted. "
                    "The artwork remains eligible for a later run."
                ),
                movement=movement_name,
                confidence=confidence,
                board_key=target_board.board_key,
                image_sha256=image_sha256,
            )

        if not await self._state_repo.reserve_artwork(canonical_id, run_id):
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=artwork.title,
                decision=PipelineDecision.ALREADY_RESERVED,
                reason=(
                    f"Artwork {canonical_id} is reserved by another run or already published. "
                    "Skipped to prevent a concurrent double write."
                ),
                movement=movement_name,
                confidence=confidence,
                image_sha256=image_sha256,
            )

        record = existing_record or PublicationRecord(artwork_canonical_id=canonical_id)
        self._apply_record_context(
            record,
            run_id=run_id,
            rights_eval=rights_eval,
            classification=classification,
            target_board=target_board,
            board_id=board_id,
            image_url=image_url,
            image_sha256=image_sha256,
            perceptual_hash=perceptual_hash,
            link=artwork.museum_object_url,
        )
        await self._state_repo.save_record(record)

        request = CreatePinRequest(
            board_id=board_id,
            title=pin_meta.title,
            description=pin_meta.description,
            link=pin_meta.link,
            alt_text=pin_meta.alt_text,
            media_source_url=image_url,
        )
        self._attach_media(request, image_bytes)

        try:
            pin = await self._client.create_pin(request)
        except PinterestAmbiguousWriteError as e:
            # INVARIANT: never retried. Parked for explicit reconciliation.
            return await self._park_ambiguous(record, canonical_id, run_id, target_board, e)
        except ProductionWriteDisabledError:
            raise
        except PinterestError as e:
            return await self._record_failure(record, canonical_id, run_id, target_board, e)

        record.pin_id = pin.id
        record.pin_url = pin.url
        record.transition_to(PublicationState.PUBLISHED, reason=f"Pin {pin.id} created")
        self.note_board_write(target_board.board_key)
        await self._state_repo.save_record(record)
        await self._state_repo.release_reservation(canonical_id, run_id)

        if perceptual_hash:
            await self._state_repo.record_perceptual_hash(perceptual_hash, canonical_id)

        logger.info(
            "publish_succeeded",
            canonical_id=canonical_id,
            pin_id=pin.id,
            board_id=board_id,
        )

        return PipelineItemResult(
            canonical_id=canonical_id,
            title=artwork.title,
            decision=PipelineDecision.PUBLISHED,
            reason=f"Pin {pin.id} published to '{target_board.board_name}'.",
            movement=movement_name,
            confidence=confidence,
            board_key=target_board.board_key,
            image_sha256=image_sha256,
            pin_id=pin.id,
        )

    def _attach_media(self, request: CreatePinRequest, image_bytes: bytes | None) -> None:
        """Upload a prepared image when configured, else keep the museum URL.

        Preparation never blocks publishing: if the image cannot be re-encoded,
        the original URL is used unchanged.
        """
        if self._preparer is None or self._preparer.mode != MediaMode.BASE64:
            return
        if not image_bytes:
            return

        prepared = self._preparer.prepare(image_bytes, source_url=request.media_source_url)
        if prepared is None:
            logger.warning("pin_media_fallback_to_url", url=request.media_source_url)
            return

        request.media_source_url = None
        request.media_base64 = prepared.to_base64()
        logger.info(
            "pin_media_uploaded",
            width=prepared.width,
            height=prepared.height,
            size_bytes=prepared.size_bytes,
            transformed=prepared.transformed,
        )

    # ── State transitions ────────────────────────────────────────────
    def _apply_record_context(
        self,
        record: PublicationRecord,
        *,
        run_id: str,
        rights_eval: RightsEvaluation,
        classification: MovementClassification,
        target_board: BoardTarget,
        board_id: str,
        image_url: str,
        image_sha256: str,
        perceptual_hash: str | None,
        link: str | None,
    ) -> None:
        """Attach this attempt's context and walk the record up to PUBLISHING.

        A record coming from FAILED or RECONCILED is re-entered from its current
        state; a fresh record walks the full documented sequence.
        """
        record.run_id = run_id
        record.rights_evaluation = rights_eval
        record.movement_classification = classification
        record.board_target = BoardTarget(
            board_key=target_board.board_key,
            board_name=target_board.board_name,
            board_id=board_id,
        )
        record.image_sha256 = image_sha256
        record.image_perceptual_hash = perceptual_hash
        record.image_url_used = image_url
        record.link = link
        record.error_message = None

        sequence = (
            (PublicationState.DISCOVERED, PublicationState.RIGHTS_APPROVED, rights_eval.reason),
            (PublicationState.RIGHTS_APPROVED, PublicationState.CLASSIFIED, classification.reason),
            (
                PublicationState.CLASSIFIED,
                PublicationState.BOARD_ASSIGNED,
                f"Board {target_board.board_name}",
            ),
            (PublicationState.BOARD_ASSIGNED, PublicationState.RESERVED, f"Reserved by {run_id}"),
        )
        for current, following, reason in sequence:
            if record.state == current:
                record.transition_to(following, reason=reason)

        record.transition_to(PublicationState.PUBLISHING, reason="Writing Pin to Pinterest")

    async def _park_ambiguous(
        self,
        record: PublicationRecord,
        canonical_id: str,
        run_id: str,
        target_board: BoardTarget,
        error: Exception,
    ) -> PipelineItemResult:
        """Park an ambiguous write. This is the only exit to AMBIGUOUS."""
        record.error_message = str(error)
        record.transition_to(PublicationState.AMBIGUOUS, reason=str(error))
        await self._state_repo.save_record(record)
        await self._state_repo.release_reservation(canonical_id, run_id)

        logger.error(
            "publish_ambiguous",
            canonical_id=canonical_id,
            board_id=target_board.board_id,
            error=str(error),
        )
        return PipelineItemResult(
            canonical_id=canonical_id,
            title=record.artwork_canonical_id,
            decision=PipelineDecision.PUBLISH_AMBIGUOUS,
            reason=(
                "Pinterest write result was ambiguous. The record is parked in "
                "AMBIGUOUS and must be reconciled before any retry."
            ),
            board_key=target_board.board_key,
            image_sha256=record.image_sha256,
            error=str(error),
        )

    async def _record_failure(
        self,
        record: PublicationRecord,
        canonical_id: str,
        run_id: str,
        target_board: BoardTarget,
        error: Exception,
    ) -> PipelineItemResult:
        """Record a definitive failure and release the reservation for a retry."""
        record.retry_count += 1
        record.error_message = str(error)
        record.transition_to(PublicationState.FAILED, reason=str(error))
        await self._state_repo.save_record(record)
        await self._state_repo.release_reservation(canonical_id, run_id)

        logger.error("publish_failed", canonical_id=canonical_id, error=str(error))
        return PipelineItemResult(
            canonical_id=canonical_id,
            title=record.artwork_canonical_id,
            decision=PipelineDecision.PUBLISH_FAILED,
            reason=f"Pinterest write failed definitively: {error}",
            board_key=target_board.board_key,
            image_sha256=record.image_sha256,
            error=str(error),
        )
