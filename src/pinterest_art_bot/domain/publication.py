"""Publication domain model.

Models the complete lifecycle of an artwork from discovery through
Pinterest publication, including the critical AMBIGUOUS state for
handling non-idempotent write failures.

State machine (simplified):
  DISCOVERED → RIGHTS_APPROVED → CLASSIFIED → BOARD_ASSIGNED
  → CURATED → PLANNED → PUBLISHING → PUBLISHED

  Any state can transition to FAILED.
  PUBLISHING can transition to AMBIGUOUS (network failure during write).
  AMBIGUOUS requires explicit reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from pinterest_art_bot.domain.movement import MovementClassification
from pinterest_art_bot.domain.rights import RightsEvaluation


class PublicationState(StrEnum):
    """Explicit publication lifecycle states.

    These are NOT boolean flags — each state has distinct semantics
    and permitted transitions.
    """

    # Discovery and evaluation
    DISCOVERED = "discovered"
    RIGHTS_APPROVED = "rights_approved"
    RIGHTS_DENIED = "rights_denied"
    CLASSIFIED = "classified"
    BOARD_ASSIGNED = "board_assigned"

    # Curation
    CURATED = "curated"
    REJECTED = "rejected"

    # Publication pipeline
    PLANNED = "planned"
    RESERVED = "reserved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"

    # Error / recovery states
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"
    RECONCILED = "reconciled"


# States that mean "done, do not re-process"
TERMINAL_STATES: frozenset[PublicationState] = frozenset(
    {
        PublicationState.PUBLISHED,
        PublicationState.RIGHTS_DENIED,
        PublicationState.REJECTED,
    }
)

# States whose artworks may be attempted again.
# FAILED means a definitive write failure, so nothing was created.
# RECONCILED means reconciliation proved no Pin exists.
# PUBLISHED and AMBIGUOUS are deliberately absent: neither may be retried.
RETRYABLE_PUBLICATION_STATES: frozenset[PublicationState] = frozenset(
    {
        PublicationState.FAILED,
        PublicationState.RECONCILED,
    }
)

# States that mean a write is currently in flight. A record abandoned in one of
# these by a crashed process has an unknown outcome and must be recovered.
IN_FLIGHT_STATES: frozenset[PublicationState] = frozenset(
    {
        PublicationState.RESERVED,
        PublicationState.PUBLISHING,
    }
)

# States that require human or automated reconciliation
REQUIRES_RECONCILIATION: frozenset[PublicationState] = frozenset(
    {
        PublicationState.AMBIGUOUS,
    }
)


class BoardTarget(BaseModel):
    """The target Pinterest board for an artwork."""

    board_key: str = Field(
        ...,
        description="Internal key matching board config, e.g. 'baroque'.",
    )
    board_name: str = Field(
        ...,
        description="Display name of the board, e.g. 'Baroque Art'.",
    )
    board_id: str | None = Field(
        default=None,
        description="Pinterest board ID. Populated after board exists on Pinterest.",
    )


class PinMetadata(BaseModel):
    """Metadata for a Pinterest Pin, generated from artwork + classification."""

    title: str
    description: str
    link: str | None = None
    alt_text: str | None = None
    board_target: BoardTarget


class InvalidStateTransitionError(Exception):
    """Raised when an illegal publication state transition is attempted."""


class PublicationRecord(BaseModel):
    """Complete record of an artwork's journey through the publication pipeline.

    This is the authoritative state for deduplication and recovery.
    """

    # ── Identity ────────────────────────────────────────────────────
    artwork_canonical_id: str = Field(
        ...,
        description="e.g. 'met:436535'",
    )
    run_id: str | None = Field(
        default=None,
        description="ID of the pipeline run that processed this artwork.",
    )

    # ── State ───────────────────────────────────────────────────────
    state: PublicationState = PublicationState.DISCOVERED
    state_history: list[StateTransition] = Field(default_factory=list)

    # ── Evaluation results ──────────────────────────────────────────
    rights_evaluation: RightsEvaluation | None = None
    movement_classification: MovementClassification | None = None
    board_target: BoardTarget | None = None

    # ── Pinterest output ────────────────────────────────────
    pin_id: str | None = Field(
        default=None,
        description="Pinterest Pin ID, set after successful publication.",
    )
    pin_url: str | None = None

    # ── Media ───────────────────────────────────────────────────────
    image_sha256: str | None = None
    image_perceptual_hash: str | None = None
    image_url_used: str | None = None
    link: str | None = Field(
        default=None,
        description="Source link submitted with the Pin, e.g. the museum object URL.",
    )

    # ── Timestamps ──────────────────────────────────────────────────
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last state change. Used to detect abandoned in-flight writes.",
    )
    published_at: datetime | None = None
    failed_at: datetime | None = None

    # ── Error tracking ──────────────────────────────────────────────
    error_message: str | None = None
    retry_count: int = 0

    def transition_to(self, new_state: PublicationState, reason: str | None = None) -> None:
        """Record a state transition with timestamp.

        Enforces legal transition rules, particularly around AMBIGUOUS and terminal states.
        """
        # INVARIANT: AMBIGUOUS requires definitive reconciliation.
        # It must NEVER be silently transitioned to FAILED or retried.
        if self.state == PublicationState.AMBIGUOUS and new_state not in (
            PublicationState.RECONCILED,
            PublicationState.PUBLISHED,
        ):
            raise InvalidStateTransitionError(
                f"Cannot transition from AMBIGUOUS to {new_state.value}. "
                "AMBIGUOUS requires definitive reconciliation (RECONCILED or PUBLISHED) "
                "and must never be silently converted to FAILED or retried blindly."
            )

        if self.state in TERMINAL_STATES and new_state != self.state:
            raise InvalidStateTransitionError(
                f"Cannot transition from terminal state {self.state.value} to {new_state.value}."
            )

        self.state_history.append(
            StateTransition(
                from_state=self.state,
                to_state=new_state,
                reason=reason,
            )
        )
        self.state = new_state
        self.updated_at = datetime.now(UTC)

        if new_state == PublicationState.PUBLISHED:
            self.published_at = datetime.now(UTC)
        elif new_state == PublicationState.FAILED:
            self.failed_at = datetime.now(UTC)


class StateTransition(BaseModel):
    """Record of a single state transition for audit trail."""

    from_state: PublicationState
    to_state: PublicationState
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None


# Pydantic v2: rebuild to resolve forward references
PublicationRecord.model_rebuild()
