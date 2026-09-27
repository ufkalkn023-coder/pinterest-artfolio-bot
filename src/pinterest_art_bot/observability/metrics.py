"""Operational metrics.

The pipeline already logs every event, but logs answer "what happened in this
process". Operators need the aggregate shape as well: how much is being
published per day, how much is stuck, how much the catalogue is blocked by the
rights policy. These metrics are derived from authoritative state, so they stay
correct even after a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from pinterest_art_bot.domain.publication import PublicationState
from pinterest_art_bot.state.repository import StateRepository


class RightsBreakdown(BaseModel):
    """How the rights gate split the last run."""

    approved: int = 0
    denied: int = 0
    review_required: int = 0


class DuplicateBreakdown(BaseModel):
    """How much was suppressed as already handled."""

    published: int = 0
    seen: int = 0
    exact_image: int = 0
    near_image: int = 0
    cross_source: int = 0


class PipelineMetrics(BaseModel):
    """Aggregate view of publication activity."""

    generated_at: datetime
    window_hours: int = 24

    published_24h: int = 0
    published_today: int = 0
    ambiguous_open: int = 0
    failed_open: int = 0
    dry_run_plans: int = 0
    known_works: int = 0

    rights: RightsBreakdown = Field(default_factory=RightsBreakdown)
    duplicates: DuplicateBreakdown = Field(default_factory=DuplicateBreakdown)
    runs_24h: int = 0
    errors_24h: int = 0

    @property
    def needs_attention(self) -> bool:
        """Whether an operator should look at this state now."""
        return self.ambiguous_open > 0 or self.errors_24h > 0

    def warnings(self) -> list[str]:
        """Human-readable alerts, suitable for logs or CLI output."""
        alerts: list[str] = []
        if self.ambiguous_open:
            alerts.append(
                f"{self.ambiguous_open} artwork(s) have ambiguous Pinterest writes that "
                "were never retried. Run 'pinterest-art-bot reconcile'."
            )
        if self.errors_24h:
            alerts.append(f"{self.errors_24h} run(s) reported processing errors in the last 24h.")
        if self.rights.review_required:
            alerts.append(
                f"{self.rights.review_required} artwork(s) in the last run are blocked by the "
                "rights policy (permissive but not auto-publishable)."
            )
        return alerts


async def collect_metrics(
    state_repo: StateRepository,
    *,
    window_hours: int = 24,
    last_run: dict[str, Any] | None = None,
) -> PipelineMetrics:
    """Build the aggregate metrics view from authoritative state."""
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=window_hours)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Counts come from SQL, so status stays cheap on a large state database.
    ambiguous_open = await state_repo.count_records_by_state(PublicationState.AMBIGUOUS)
    failed_open = await state_repo.count_records_by_state(PublicationState.FAILED)
    dry_run_plans = await state_repo.count_dry_run_plans()
    known_works = await state_repo.count_work_identities()

    runs = await state_repo.list_runs(limit=500)
    recent_runs = [run for run in runs if _within(run.get("finished_at"), window_start)]

    metrics = PipelineMetrics(
        generated_at=now,
        window_hours=window_hours,
        published_24h=await state_repo.count_published_since(window_start),
        published_today=await state_repo.count_published_since(day_start),
        ambiguous_open=ambiguous_open,
        failed_open=failed_open,
        dry_run_plans=dry_run_plans,
        known_works=known_works,
        runs_24h=len(recent_runs),
        errors_24h=sum(int(run.get("errors", 0) or 0) for run in recent_runs),
    )

    if last_run:
        metrics.rights = RightsBreakdown(
            approved=int(last_run.get("rights_approved", 0) or 0),
            denied=int(last_run.get("rights_rejected", 0) or 0),
            review_required=int(last_run.get("rights_review_required", 0) or 0),
        )
        metrics.duplicates = DuplicateBreakdown(
            published=int(last_run.get("duplicates_published", 0) or 0),
            seen=int(last_run.get("duplicates_seen", 0) or 0),
            exact_image=int(last_run.get("duplicates_image_hash", 0) or 0),
            near_image=int(last_run.get("duplicates_near_match", 0) or 0),
            cross_source=int(last_run.get("duplicates_cross_source", 0) or 0),
        )

    return metrics


def _within(value: object, cutoff: datetime) -> bool:
    """Whether an ISO timestamp is at or after the cutoff."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= cutoff
