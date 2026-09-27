"""Pre-publication checks.

Turning on `PUBLISH_TO_PINTEREST` is a one-line change with real consequences.
This command gathers the evidence needed to make that decision deliberately:
configuration validity, board readiness, live museum reachability, observed
dry-run yield, and the leftover work that needs attention.

It never writes to Pinterest.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, Field, computed_field

from pinterest_art_bot.media.preparation import aspect_ratio_is_preferred
from pinterest_art_bot.museums.base import MuseumProvider
from pinterest_art_bot.pipeline.models import DiscoveryMode, DiscoveryRequest
from pinterest_art_bot.state.repository import StateRepository

logger = structlog.get_logger()

# Yield below this suggests the pipeline is safe but not yet useful.
LOW_YIELD_THRESHOLD = 0.10


class CheckStatus(StrEnum):
    """Outcome of a single preflight check."""

    OK = "ok"
    WARNING = "warning"
    BLOCKER = "blocker"
    SKIPPED = "skipped"


class Check(BaseModel):
    """One preflight finding."""

    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None


class PreflightReport(BaseModel):
    """Everything needed to decide whether to enable publishing."""

    museum: str
    checks: list[Check] = Field(default_factory=list)
    fetched: int = 0
    publishable_share: float = 0.0
    blockers: int = 0
    warnings: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> bool:
        """Whether publishing can be enabled right now."""
        return self.blockers == 0

    def add(self, name: str, status: CheckStatus, detail: str, remedy: str | None = None) -> None:
        """Record a finding and update the tallies."""
        self.checks.append(Check(name=name, status=status, detail=detail, remedy=remedy))
        if status == CheckStatus.BLOCKER:
            self.blockers += 1
        elif status == CheckStatus.WARNING:
            self.warnings += 1


async def run_preflight(
    *,
    app: Any,
    museum: str | None = None,
    limit: int = 10,
    probe_pinterest: bool = True,
) -> PreflightReport:
    """Run every pre-publication check. Read-only throughout."""
    settings = app.settings
    report = PreflightReport(museum=museum or settings.default_museum)

    _check_switches(report, settings)
    _check_policy(report, settings)
    _check_boards(report, app)
    await _check_state(report, app.state_repo)
    await _check_museum(report, app, report.museum, limit)
    await _check_pinterest(report, app, probe_pinterest)

    return report


def _check_switches(report: PreflightReport, settings: Any) -> None:
    """Report exactly which production switches are on."""
    on = [
        name
        for name in (
            "publish_to_pinterest",
            "pinterest_auto_create_boards",
            "pinterest_production_schedule_enabled",
        )
        if getattr(settings, name)
    ]
    if on:
        report.add(
            "production switches",
            CheckStatus.WARNING,
            f"already enabled: {', '.join(on)}",
            "Publishing is live. Confirm this was intended.",
        )
    else:
        report.add(
            "production switches",
            CheckStatus.OK,
            "all production switches are OFF (dry-run only)",
        )

    report.add(
        "credentials",
        CheckStatus.OK if settings.is_pinterest_configured else CheckStatus.BLOCKER,
        "access token present" if settings.is_pinterest_configured else "no access token",
        None if settings.is_pinterest_configured else "Set PINTEREST_ACCESS_TOKEN to publish.",
    )
    if settings.can_publish:
        report.add(
            "publish capability",
            CheckStatus.OK,
            "can_publish is true; writes would be attempted",
        )


def _check_policy(report: PreflightReport, settings: Any) -> None:
    """Show the rights policy in force and its strictness."""
    statuses = sorted(status.value for status in settings.auto_publishable_rights_statuses)
    report.add(
        "rights policy",
        CheckStatus.OK if settings.rights_enforce_strict else CheckStatus.BLOCKER,
        f"auto-publishable: {', '.join(statuses)}; strict={settings.rights_enforce_strict}",
        None
        if settings.rights_enforce_strict
        else "Re-enable RIGHTS_ENFORCE_STRICT before publishing.",
    )

    budget = settings.effective_max_pins_per_run
    if budget == 0:
        report.add(
            "pin budget",
            CheckStatus.WARNING,
            "no Pins may be written (per-run or daily cap is 0)",
            "Raise PINTEREST_MAX_PINS_PER_RUN to publish anything.",
        )
    else:
        report.add(
            "pin budget",
            CheckStatus.OK,
            f"up to {budget} Pins per run, {settings.pinterest_max_pins_per_day} per day",
        )


def _check_boards(report: PreflightReport, app: Any) -> None:
    """Board configuration must be complete and resolved before any write."""
    boards = app.boards_config.boards
    enabled = [board for board in boards if board.enabled]
    unresolved = [board.board_key for board in enabled if not board.board_id]

    if not enabled:
        report.add(
            "board config",
            CheckStatus.BLOCKER,
            "no enabled boards in configuration",
            "Enable at least one board in config/boards.yaml.",
        )
        return

    if unresolved:
        report.add(
            "board config",
            CheckStatus.BLOCKER,
            f"{len(unresolved)} of {len(enabled)} boards have no Pinterest board ID",
            "Run 'pinterest-art-bot boards-sync' to resolve board IDs.",
        )
    else:
        report.add(
            "board config",
            CheckStatus.OK,
            f"all {len(enabled)} enabled boards have a Pinterest board ID",
        )


async def _check_state(report: PreflightReport, state_repo: StateRepository) -> None:
    """Report open publication work that needs attention first."""
    from pinterest_art_bot.domain.publication import PublicationState

    ambiguous = await state_repo.count_records_by_state(PublicationState.AMBIGUOUS)
    failed = await state_repo.count_records_by_state(PublicationState.FAILED)
    published = await state_repo.count_records_by_state(PublicationState.PUBLISHED)

    if ambiguous:
        report.add(
            "open ambiguous writes",
            CheckStatus.BLOCKER,
            f"{ambiguous} record(s) await reconciliation",
            "Run 'pinterest-art-bot reconcile' before publishing.",
        )
    else:
        report.add("open ambiguous writes", CheckStatus.OK, "none")

    if failed:
        report.add(
            "failed records",
            CheckStatus.WARNING,
            f"{failed} record(s) eligible for retry",
            "Run 'pinterest-art-bot retry' to re-attempt them.",
        )

    report.add(
        "publication history",
        CheckStatus.OK,
        f"{published} Pin(s) published so far",
    )


async def _check_museum(report: PreflightReport, app: Any, museum: str, limit: int) -> None:
    """Probe the museum and measure what actually reaches publication."""
    provider: MuseumProvider | None = app.runner.providers.get(museum)
    if provider is None:
        report.add(
            "museum reachability",
            CheckStatus.BLOCKER,
            f"'{museum}' is not available",
            f"Configured sources: {', '.join(app.runner.providers)}",
        )
        return

    try:
        summary = await app.runner.run(
            DiscoveryRequest(
                museum=museum,
                limit=limit,
                mode=DiscoveryMode.HIGHLIGHTS,
                publish=False,
            )
        )
    except Exception as e:
        report.add(
            "museum reachability",
            CheckStatus.BLOCKER,
            f"discovery failed: {e}",
            "Check network access and the API status page.",
        )
        return

    report.add(
        "museum reachability",
        CheckStatus.OK,
        f"{summary.fetched} artwork(s) fetched from {museum}",
    )
    report.fetched = summary.fetched
    if summary.errors:
        report.add(
            "discovery errors",
            CheckStatus.WARNING,
            f"{summary.errors} item(s) errored",
        )

    ready = summary.ready_dry_run_plans
    share = (ready / summary.fetched) if summary.fetched else 0.0
    report.publishable_share = round(share, 3)
    report.add(
        "dry-run yield",
        CheckStatus.OK if share >= LOW_YIELD_THRESHOLD else CheckStatus.WARNING,
        f"{ready}/{summary.fetched} reached a publishable plan ({share:.0%})",
        None
        if share >= LOW_YIELD_THRESHOLD
        else (
            "Low yield is expected early on: the strict rights policy and the "
            "HIGH-confidence classification bar reject most of a museum catalogue. "
            "Run more museums and pages before judging."
        ),
    )

    _check_classification(report, summary)

    if summary.rights_review_required:
        report.add(
            "policy-blocked artwork",
            CheckStatus.WARNING,
            f"{summary.rights_review_required} item(s) blocked by the rights policy",
            "These can never be published automatically; that is intentional.",
        )
    if summary.duplicates_cross_source:
        report.add(
            "cross-source duplicates",
            CheckStatus.OK,
            f"{summary.duplicates_cross_source} duplicate work item(s) suppressed",
        )


def _check_classification(report: PreflightReport, summary: Any) -> None:
    """Name the actual bottleneck. A low yield number alone is not a diagnosis."""
    if summary.image_invalid:
        share = summary.image_invalid / summary.fetched if summary.fetched else 0
        report.add(
            "image fetch",
            CheckStatus.WARNING if share < 0.5 else CheckStatus.BLOCKER,
            f"{summary.image_invalid}/{summary.fetched} image(s) unusable ({share:.0%})",
            (
                "Unusable images are usually a museum image URL that is dead, "
                "redirects, or blocks hotlinking. Check the adapter's image "
                "resolution before publishing."
            ),
        )

    if summary.held_for_review and not summary.high_confidence_classified:
        report.add(
            "classification confidence",
            CheckStatus.WARNING,
            (
                f"0 of {summary.held_for_review} classified item(s) reached HIGH "
                "confidence; all were held for review"
            ),
            (
                "This, not rights, is the usual reason for a zero yield. Movement "
                "classification is deliberately conservative: only HIGH confidence "
                "auto-routes. Run a larger sample before treating this as a fault; "
                "if confidence is chronically borderline, fix classification/aliases.py "
                "rather than lowering the bar."
            ),
        )


async def _check_pinterest(report: PreflightReport, app: Any, probe: bool) -> None:
    """Read-only check of the Pinterest connection."""
    client = app.pinterest_client
    if client is None:
        report.add(
            "pinterest connection",
            CheckStatus.SKIPPED,
            "no credentials configured",
            "Set PINTEREST_ACCESS_TOKEN, then run boards-sync.",
        )
        return

    if not probe:
        report.add("pinterest connection", CheckStatus.SKIPPED, "probe not requested")
        return

    from pinterest_art_bot.pinterest.errors import PinterestError

    try:
        boards = await client.list_boards()
    except PinterestError as e:
        report.add(
            "pinterest connection",
            CheckStatus.BLOCKER,
            f"could not read boards: {e}",
            "Verify the token is valid and not expired.",
        )
        return

    report.add(
        "pinterest connection",
        CheckStatus.OK,
        f"token valid; {len(boards)} board(s) visible",
    )
    if not boards:
        report.add(
            "pinterest boards",
            CheckStatus.WARNING,
            "account has no boards",
            "Create boards manually or enable PINTEREST_AUTO_CREATE_BOARDS.",
        )


async def check_image_fits(artwork: Any) -> bool:
    """Whether an artwork's image is close to Pinterest's preferred aspect ratio."""
    image = artwork.primary_image
    if not image or not image.width or not image.height:
        return False
    return aspect_ratio_is_preferred(image.width, image.height)
