"""Pipeline domain models for ingestion, evaluation, and publication execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from pinterest_art_bot.domain.publication import PinMetadata


class DiscoveryMode(StrEnum):
    """Discovery strategy for museum queries."""

    HIGHLIGHTS = "highlights"
    SEARCH = "search"
    OBJECT_ID = "object_id"


class DiscoveryRequest(BaseModel):
    """Configuration for a museum artwork discovery query."""

    museum: str = Field(description="Museum provider source id, e.g. 'artic' or 'met'")
    limit: int = Field(default=10, ge=1, le=100)
    mode: DiscoveryMode = DiscoveryMode.HIGHLIGHTS
    query: str | None = None
    object_ids: list[str] = Field(default_factory=list)
    public_domain_only: bool = True
    require_image: bool = True
    offset: int = Field(default=0, ge=0)
    rotate: bool = Field(
        default=True,
        description=(
            "Resume from the persisted discovery cursor when no explicit offset is given, "
            "so successive runs explore different parts of the collection."
        ),
    )
    publish: bool = Field(
        default=False,
        description="Execute real Pinterest writes. Requires PUBLISH_TO_PINTEREST=true.",
    )
    max_pages: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum discovery pages to fetch before stopping.",
    )
    page_size: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Page size used for each museum request.",
    )


class PipelineDecision(StrEnum):
    """Deterministic terminal decision for an artwork in the pipeline."""

    READY = "ready"
    RIGHTS_DENIED = "rights_denied"
    RIGHTS_REVIEW_REQUIRED = "rights_review_required"
    NO_IMAGE = "no_image"
    IMAGE_INVALID = "image_invalid"
    ALREADY_SEEN = "already_seen"
    ALREADY_PUBLISHED = "already_published"
    DUPLICATE_IMAGE_HASH = "duplicate_image_hash"
    DUPLICATE_NEAR_MATCH = "duplicate_near_match"
    DUPLICATE_WORK = "duplicate_work"
    ALREADY_RESERVED = "already_reserved"
    CLASSIFICATION_UNCERTAIN = "classification_uncertain"
    NO_BOARD_MAPPING = "no_board_mapping"
    BOARD_ID_MISSING = "board_id_missing"
    ROUTING_HELD = "routing_held"
    PUBLISHED = "published"
    PUBLISH_AMBIGUOUS = "publish_ambiguous"
    PUBLISH_FAILED = "publish_failed"
    PIN_BUDGET_EXHAUSTED = "pin_budget_exhausted"
    BOARD_BUDGET_EXHAUSTED = "board_budget_exhausted"
    ERROR = "error"


class DryRunPlan(BaseModel):
    """Structured publication plan produced during a dry-run.

    Crucially, board_id is None until synchronized with Pinterest.
    publishable is False to guarantee no downstream writer acts upon it.
    """

    artwork_id: str
    title: str
    artist: str | None = None
    movement: str
    movement_confidence: float
    confidence_level: str
    board_key: str
    board_name: str
    board_id: str | None = None  # None until remote board is synced
    image_url: str
    image_sha256: str
    museum_url: str | None = None
    rights_status: str
    rights_reason: str
    publishable: bool = False
    dry_run: bool = True
    pin_metadata: PinMetadata | None = None


class PipelineItemResult(BaseModel):
    """The result of processing a single artwork through the pipeline."""

    canonical_id: str
    title: str
    decision: PipelineDecision
    reason: str
    movement: str | None = None
    confidence: float | None = None
    board_key: str | None = None
    image_sha256: str | None = None
    plan: DryRunPlan | None = None
    pin_id: str | None = None
    error: str | None = None
    raw_details: dict[str, Any] = Field(default_factory=dict)


class PipelineRunSummary(BaseModel):
    """Consolidated summary of a pipeline execution run."""

    run_id: str
    museum: str
    dry_run: bool = True
    fetched: int = 0
    rights_approved: int = 0
    rights_rejected: int = 0
    rights_review_required: int = 0
    image_valid: int = 0
    image_invalid: int = 0
    duplicates_seen: int = 0
    duplicates_published: int = 0
    duplicates_image_hash: int = 0
    duplicates_near_match: int = 0
    duplicates_cross_source: int = 0
    reserved_conflicts: int = 0
    retries_considered: int = 0
    high_confidence_classified: int = 0
    held_for_review: int = 0
    board_routed: int = 0
    board_id_missing: int = 0
    board_budget_exhausted: int = 0
    ready_dry_run_plans: int = 0
    published: int = 0
    publish_ambiguous: int = 0
    publish_failed: int = 0
    errors: int = 0
    pinterest_writes: int = 0  # always 0 in dry-run mode
    items: list[PipelineItemResult] = Field(default_factory=list)
    plans: list[DryRunPlan] = Field(default_factory=list)

    def count_decision(self, decision: PipelineDecision) -> int:
        """How many items ended with the given decision."""
        return sum(1 for item in self.items if item.decision == decision)
