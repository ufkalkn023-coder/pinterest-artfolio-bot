"""Command-line interface for pinterest-art-bot.

Commands:
    status        show safety switches, museum sources, and board configuration
    ingest        discover, verify, classify, and plan (or publish)
    boards-sync   resolve Pinterest board IDs into config/boards.yaml
    reconcile     resolve AMBIGUOUS writes left by network failures
    state         inspect publication state
    schedule      run the gated production loop

Every production write is guarded twice: by the safety switches in settings and
by the SafePinterestClient wrapper. The CLI never bypasses either.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from pinterest_art_bot.bootstrap import Application, build_application, require_pinterest_client
from pinterest_art_bot.config.settings import Settings, get_settings
from pinterest_art_bot.domain.publication import (
    RETRYABLE_PUBLICATION_STATES,
    PublicationRecord,
    PublicationState,
)
from pinterest_art_bot.logging.setup import setup_logging
from pinterest_art_bot.observability.metrics import collect_metrics
from pinterest_art_bot.pinterest.boards_sync import sync_board_ids, write_boards_config
from pinterest_art_bot.pinterest.errors import PinterestError
from pinterest_art_bot.pinterest.reconciliation import AmbiguousWriteReconciler
from pinterest_art_bot.pipeline.lock import LockUnavailableError
from pinterest_art_bot.pipeline.models import DiscoveryMode, DiscoveryRequest
from pinterest_art_bot.pipeline.scheduler import ScheduleDisabledError, Scheduler

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BLOCKED = 2
EXIT_ERROR = 3


# ── Parser ───────────────────────────────────────────────────────────


def parse_discovery_mode(value: str) -> DiscoveryMode:
    """Parse a discovery mode, accepting hyphenated or underscored names."""
    normalized = value.strip().lower().replace("-", "_")
    try:
        return DiscoveryMode(normalized)
    except ValueError as e:
        choices = ", ".join(mode.value for mode in DiscoveryMode)
        raise argparse.ArgumentTypeError(
            f"invalid discovery mode: '{value}' (choose from {choices})"
        ) from e


def create_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pinterest-art-bot",
        description="Pinterest Art Bot — Museum Ingestion & Art Intelligence Pipeline",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override log level (DEBUG, INFO, WARNING, ERROR).",
    )
    parser.add_argument(
        "--log-format",
        default=None,
        choices=["json", "console"],
        help="Override log format.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    preflight = subparsers.add_parser(
        "preflight",
        help="Check whether publishing is safe to enable. Never writes to Pinterest.",
    )
    preflight.add_argument(
        "--museum", default=None, help="Museum to probe (default: from settings)."
    )
    preflight.add_argument("--limit", type=int, default=10, help="Artworks to probe (default: 10).")
    preflight.add_argument(
        "--skip-pinterest",
        action="store_true",
        default=False,
        help="Do not contact Pinterest, even when a token is configured.",
    )
    preflight.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    status = subparsers.add_parser("status", help="Show safety switches and configuration state.")
    status.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    ingest = subparsers.add_parser(
        "ingest",
        help="Discover artworks, verify rights, classify, and plan or publish.",
    )
    ingest.add_argument("--museum", default=None, help="Museum source id (default: from settings)")
    ingest.add_argument("--limit", type=int, default=None, help="Artworks to process this run.")
    ingest.add_argument(
        "--mode",
        type=parse_discovery_mode,
        default=DiscoveryMode.HIGHLIGHTS,
        help="Discovery mode: highlights, search, or object-id (default: highlights).",
    )
    ingest.add_argument("--query", default=None, help="Search query (requires --mode search).")
    ingest.add_argument(
        "--object-ids",
        nargs="*",
        default=None,
        help="Specific object IDs (requires --mode object-id).",
    )
    ingest.add_argument("--page-size", type=int, default=None, help="Museum page size.")
    ingest.add_argument("--max-pages", type=int, default=None, help="Maximum pages per run.")
    ingest.add_argument("--offset", type=int, default=0, help="Starting discovery offset.")
    ingest.add_argument(
        "--publish",
        action="store_true",
        default=False,
        help="Write Pins to Pinterest. Requires PUBLISH_TO_PINTEREST=true and a token.",
    )
    ingest.add_argument(
        "--allow-non-public-domain",
        action="store_true",
        default=False,
        help="Disable the public-domain discovery filter (rights gate still applies).",
    )
    ingest.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    boards = subparsers.add_parser(
        "boards-sync",
        help="Resolve Pinterest board IDs and persist them to the board config.",
    )
    boards.add_argument(
        "--create",
        action="store_true",
        default=False,
        help="Create boards that do not exist yet (requires PINTEREST_AUTO_CREATE_BOARDS=true).",
    )
    boards.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Resolve AMBIGUOUS writes by inspecting Pinterest.",
    )
    reconcile.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum ambiguous records to examine (default: from settings).",
    )
    reconcile.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    retry = subparsers.add_parser(
        "retry",
        help="Re-attempt artworks whose previous attempt definitively failed.",
    )
    retry.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum records to retry (default: 25).",
    )
    retry.add_argument(
        "--state",
        choices=[*sorted(state.value for state in RETRYABLE_PUBLICATION_STATES), "all"],
        default="all",
        help="Which retryable state to re-attempt (default: all).",
    )
    retry.add_argument(
        "--publish",
        action="store_true",
        default=False,
        help="Write Pins for retried artworks. Requires PUBLISH_TO_PINTEREST=true.",
    )
    retry.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    state = subparsers.add_parser("state", help="Inspect publication state.")
    state.add_argument(
        "action",
        choices=["counts", "list", "ambiguous", "dry-runs", "runs"],
        help="What to show.",
    )
    state.add_argument("--limit", type=int, default=20, help="Maximum records to list.")
    state.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    schedule = subparsers.add_parser(
        "schedule",
        help="Run the interval loop (requires PINTEREST_PRODUCTION_SCHEDULE_ENABLED=true).",
    )
    schedule.add_argument("--museum", default=None, help="Museum source id for scheduled runs.")
    schedule.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Stop after N iterations (default: from settings).",
    )

    return parser


def _apply_logging(settings: Settings, args: argparse.Namespace) -> None:
    setup_logging(
        level=args.log_level or settings.log_level,
        fmt=args.log_format or settings.log_format,
    )


# ── Commands ─────────────────────────────────────────────────────────


def run_preflight(app: Application, args: argparse.Namespace) -> int:
    """Run the pre-publication checks and report whether publishing is safe."""
    from pinterest_art_bot.pipeline.preflight import CheckStatus
    from pinterest_art_bot.pipeline.preflight import run_preflight as collect

    report = asyncio.run(
        collect(
            app=app,
            museum=args.museum,
            limit=args.limit,
            probe_pinterest=not args.skip_pinterest,
        )
    )

    if args.json:
        print(report.model_dump_json(indent=2))
        return EXIT_OK if report.ready else EXIT_BLOCKED

    print("=" * 60)
    print(" PINTEREST ART BOT — PREFLIGHT")
    print("=" * 60)
    print(f" Museum probed: {report.museum}")
    for check in report.checks:
        marker = {
            CheckStatus.OK: "PASS",
            CheckStatus.WARNING: "WARN",
            CheckStatus.BLOCKER: "BLOCK",
            CheckStatus.SKIPPED: "SKIP",
        }[check.status]
        print(f" [{marker}] {check.name}: {check.detail}")
        if check.remedy:
            print(f"        → {check.remedy}")
    print("-" * 60)
    print(f" Dry-run yield: {report.publishable_share:.0%} of {report.fetched} fetched")
    print(f" Blockers: {report.blockers}   Warnings: {report.warnings}")
    print("=" * 60)
    if report.ready:
        print(
            " Result: no blockers. To publish, set PUBLISH_TO_PINTEREST=true and a\n"
            "         PINTEREST_ACCESS_TOKEN, then run a single-artwork canary:\n"
            "           pinterest-art-bot ingest --publish --limit 1"
        )
    else:
        print(" Result: BLOCKED. Resolve the blockers above before publishing.")
    return EXIT_OK if report.ready else EXIT_BLOCKED


def run_status(app: Application, args: argparse.Namespace) -> int:
    """Report safety switches, providers, boards, and state location."""
    settings = app.settings
    payload: dict[str, Any] = {
        "safety_switches": {
            "publish_to_pinterest": settings.publish_to_pinterest,
            "pinterest_auto_create_boards": settings.pinterest_auto_create_boards,
            "pinterest_production_schedule_enabled": (
                settings.pinterest_production_schedule_enabled
            ),
            "rights_enforce_strict": settings.rights_enforce_strict,
        },
        "capabilities": {
            "pinterest_configured": settings.is_pinterest_configured,
            "can_publish": settings.can_publish,
            "can_create_boards": settings.can_create_boards,
            "can_schedule": settings.can_schedule,
        },
        "limits": {
            "max_pins_per_run": settings.effective_max_pins_per_run,
            "max_pins_per_day": settings.pinterest_max_pins_per_day,
            "default_museum": settings.default_museum,
            "default_limit": settings.default_museum_limit,
        },
        "retention": {
            "dry_run_plans_seconds": settings.state_dry_run_retention_seconds,
            "runs_seconds": settings.state_run_retention_seconds,
            "rejected_fingerprints_seconds": settings.state_fingerprint_retention_seconds,
        },
        "reconcile": {
            "max_records": settings.reconcile_max_records,
            "max_pages_per_board": settings.reconcile_max_pages_per_board,
        },
        "paths": {
            "state_db": str(settings.state_db_file),
            "boards_config": str(settings.boards_config_file),
        },
        "museums": app.registry.source_ids,
        "boards": {
            "total": len(app.boards_config.boards),
            "with_board_id": sum(1 for b in app.boards_config.boards if b.board_id),
        },
    }

    metrics = asyncio.run(collect_metrics(app.state_repo))
    payload["metrics"] = metrics.model_dump(mode="json")
    payload["warnings"] = metrics.warnings()

    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    print("=" * 60)
    print(" PINTEREST ART BOT — STATUS")
    print("=" * 60)
    print(" Safety switches")
    for key, value in payload["safety_switches"].items():
        print(f"   {key:<38} {value}")
    print(" Capabilities")
    for key, value in payload["capabilities"].items():
        print(f"   {key:<38} {value}")
    print(" Limits")
    for key, value in payload["limits"].items():
        print(f"   {key:<38} {value}")
    print(" Paths")
    for key, value in payload["paths"].items():
        print(f"   {key:<38} {value}")
    print(f" Museums: {', '.join(payload['museums']) or 'none'}")
    print(
        f" Boards: {payload['boards']['with_board_id']}/{payload['boards']['total']} "
        "have a Pinterest board ID"
    )
    print(" Activity (24h)")
    print(f"   published_24h                    {metrics.published_24h}")
    print(f"   runs_24h                         {metrics.runs_24h}")
    print(f"   errors_24h                       {metrics.errors_24h}")
    print(f"   ambiguous_open                   {metrics.ambiguous_open}")
    print(f"   failed_open                      {metrics.failed_open}")
    print(f"   dry_run_plans                    {metrics.dry_run_plans}")
    print(f"   known_works                      {metrics.known_works}")
    print("=" * 60)
    for warning in metrics.warnings():
        print(f" WARNING: {warning}")
    return EXIT_OK


def run_ingest(app: Application, args: argparse.Namespace) -> int:
    """Execute a discovery run, publishing only when explicitly requested."""
    settings = app.settings
    mode: DiscoveryMode = args.mode

    if mode == DiscoveryMode.SEARCH and not args.query:
        sys.stderr.write("ERROR: --mode search requires --query.\n")
        return EXIT_USAGE
    if mode == DiscoveryMode.OBJECT_ID and not args.object_ids:
        sys.stderr.write("ERROR: --mode object-id requires --object-ids.\n")
        return EXIT_USAGE
    if args.publish and not settings.can_publish:
        sys.stderr.write(
            "ERROR: --publish requires PUBLISH_TO_PINTEREST=true and a configured "
            "PINTEREST_ACCESS_TOKEN. Publishing stays disabled.\n"
        )
        return EXIT_BLOCKED

    request = DiscoveryRequest(
        museum=args.museum or settings.default_museum,
        limit=args.limit or settings.default_museum_limit,
        mode=mode,
        query=args.query,
        object_ids=list(args.object_ids or []),
        public_domain_only=not args.allow_non_public_domain,
        require_image=True,
        offset=args.offset,
        publish=args.publish,
        max_pages=args.max_pages or settings.pipeline_max_pages,
        page_size=args.page_size or 25,
    )

    summary = asyncio.run(app.runner.run(request))

    if args.json:
        print(summary.model_dump_json(indent=2))
        return EXIT_OK if summary.errors == 0 else EXIT_ERROR

    print()
    print("=" * 60)
    print(" PINTEREST ART BOT — INGESTION SUMMARY")
    print("=" * 60)
    print(f" Run ID:                         {summary.run_id}")
    print(f" Museum:                         {summary.museum}")
    print(f" Mode:                           {'DRY-RUN' if summary.dry_run else 'PUBLISH'}")
    print(f" Candidates Fetched:             {summary.fetched}")
    print(f" Rights Approved:                {summary.rights_approved}")
    print(f" Rights Rejected:                {summary.rights_rejected}")
    print(f" Rights Review Required:         {summary.rights_review_required}")
    print(f" Images Valid:                   {summary.image_valid}")
    print(f" Images Invalid:                 {summary.image_invalid}")
    print(f" Duplicates (Already Published): {summary.duplicates_published}")
    print(f" Duplicates (Already Seen):      {summary.duplicates_seen}")
    print(f" Duplicates (Exact Image Hash):  {summary.duplicates_image_hash}")
    print(f" Duplicates (Near Match):        {summary.duplicates_near_match}")
    print(f" Duplicates (Other Museum):      {summary.duplicates_cross_source}")
    print(f" Reservation Conflicts:          {summary.reserved_conflicts}")
    print(f" Retryable Prior Attempts:       {summary.retries_considered}")
    print(f" Classified (HIGH Confidence):   {summary.high_confidence_classified}")
    print(f" Held for Review:                {summary.held_for_review}")
    print(f" Board Routable:                 {summary.board_routed}")
    print(f" Board ID Missing:               {summary.board_id_missing}")
    print(f" Board Budget Exhausted:         {summary.board_budget_exhausted}")
    print(f" Ready Dry-Run Plans:            {summary.ready_dry_run_plans}")
    print(f" Published:                      {summary.published}")
    print(f" Ambiguous (needs reconcile):    {summary.publish_ambiguous}")
    print(f" Publish Failures:               {summary.publish_failed}")
    print(f" Processing Errors:              {summary.errors}")
    print(f" Pinterest Writes (POST /pins):  {summary.pinterest_writes}")
    print("=" * 60)

    if summary.plans:
        print()
        print("READY DRY-RUN PUBLICATION PLANS:")
        print("-" * 60)
        for i, plan in enumerate(summary.plans, 1):
            artist_str = plan.artist or "Unknown Artist"
            print(f' [{i}] "{plan.title}" — {artist_str}')
            print(f"     Artwork ID:  {plan.artwork_id}")
            print(
                f"     Movement:    {plan.movement} "
                f"(confidence: {plan.movement_confidence:.2f}, {plan.confidence_level})"
            )
            print(
                f"     Target:      {plan.board_name} [key: {plan.board_key}, id: {plan.board_id}]"
            )
            print(f"     Rights:      {plan.rights_status} ({plan.rights_reason})")
            print(f"     Image SHA:   {plan.image_sha256[:16]}...")
            print()

    if summary.published:
        print()
        print("PUBLISHED PINS:")
        print("-" * 60)
        for item in summary.items:
            if item.pin_id:
                print(f' [{item.canonical_id}] "{item.title}" → pin {item.pin_id}')
        print()

    if summary.publish_ambiguous:
        print(
            "WARNING: some writes were ambiguous and were NOT retried. "
            "Run 'pinterest-art-bot reconcile' to resolve them."
        )
        print()

    return EXIT_OK if summary.errors == 0 else EXIT_ERROR


def run_boards_sync(app: Application, args: argparse.Namespace) -> int:
    """Resolve board IDs from Pinterest and persist them to the board config."""
    settings = app.settings
    client = require_pinterest_client(settings)
    path = settings.boards_config_file

    config = load_config(app)
    report = asyncio.run(sync_board_ids(config, client, create_missing=args.create))
    write_boards_config(config, path)

    if args.json:
        print(report.model_dump_json(indent=2))
        return EXIT_OK

    print("=" * 60)
    print(" PINTEREST ART BOT — BOARD SYNC")
    print("=" * 60)
    print(f" Remote boards:      {report.total_remote}")
    print(f" Matched:            {report.matched_count}")
    print(f" Unmatched:          {report.unresolved_count}")
    if report.unmatched_keys:
        print(f"   {', '.join(report.unmatched_keys)}")
    if report.disabled_keys:
        print(f" Disabled in config: {', '.join(report.disabled_keys)}")
    if report.remote_boards_without_config:
        print(f" Remote-only boards: {', '.join(report.remote_boards_without_config)}")
    if report.ambiguous_names:
        print(f" Ambiguous names (skipped): {', '.join(report.ambiguous_names)}")
        print(" Pinterest has boards with identical names; rename one, then re-run sync.")
    print(f" Config written to:  {path}")
    print("=" * 60)
    return EXIT_OK


def run_reconcile(app: Application, args: argparse.Namespace) -> int:
    """Resolve AMBIGUOUS records, within a bounded work budget."""
    settings = app.settings
    client = require_pinterest_client(settings)
    reconciler = AmbiguousWriteReconciler(
        client,
        app.state_repo,
        max_records=args.limit or settings.reconcile_max_records,
        max_pages_per_board=settings.reconcile_max_pages_per_board,
    )
    summary = asyncio.run(reconciler.reconcile_all())

    if args.json:
        print(summary.model_dump_json(indent=2))
        return EXIT_OK

    print("=" * 60)
    print(" PINTEREST ART BOT — RECONCILIATION")
    print("=" * 60)
    print(f" Ambiguous records examined: {summary.examined}")
    print(f" Resolved as published:     {summary.published}")
    print(f" Resolved as not published: {summary.reconciled}")
    print(f" Manual review required:    {summary.manual_review_required}")
    print(f" Left for a later pass:     {summary.remaining}")
    for result in summary.results:
        print(f"   {result.artwork_id}: {result.outcome.value} — {result.reason}")
    print("=" * 60)
    if summary.incomplete:
        print(
            f" {summary.remaining} record(s) remain. Re-run reconcile to continue "
            "(raise --limit for a larger batch)."
        )
    return EXIT_OK


def run_retry(app: Application, args: argparse.Namespace) -> int:
    """Re-attempt artworks whose previous attempt definitively failed.

    Only FAILED and RECONCILED records are eligible. AMBIGUOUS records are never
    retried here — they must be resolved with `reconcile` first.
    """
    settings = app.settings
    publish = bool(args.publish)
    if publish and not settings.can_publish:
        sys.stderr.write(
            "ERROR: --publish requires PUBLISH_TO_PINTEREST=true and a configured "
            "PINTEREST_ACCESS_TOKEN. Publishing stays disabled.\n"
        )
        return EXIT_BLOCKED

    records = asyncio.run(_retryable_records(app, limit=args.limit, state=args.state))

    if not records:
        message = "No retryable records found. Nothing to do."
        if args.json:
            print(json.dumps({"retried": 0, "message": message}, indent=2))
        else:
            print(message)
        return EXIT_OK

    # Group by museum: each source is re-fetched by its own object IDs.
    by_museum: dict[str, list[str]] = {}
    for record in records:
        source, _, object_id = record.artwork_canonical_id.partition(":")
        by_museum.setdefault(source, []).append(object_id)

    if source not in app.runner.providers:
        sys.stderr.write(
            f"ERROR: '{source}' is not available in this configuration "
            f"(museums: {', '.join(app.runner.providers)}).\n"
        )
        return EXIT_USAGE

    summaries = []
    for museum, object_ids in by_museum.items():
        request = DiscoveryRequest(
            museum=museum,
            limit=len(object_ids),
            mode=DiscoveryMode.OBJECT_ID,
            object_ids=object_ids,
            publish=publish,
            rotate=False,
        )
        summaries.append(asyncio.run(app.runner.run(request)))

    published = sum(s.published for s in summaries)
    still_failed = sum(s.publish_failed for s in summaries)
    ambiguous = sum(s.publish_ambiguous for s in summaries)
    fetched = sum(s.fetched for s in summaries)

    if args.json:
        print(
            json.dumps(
                {
                    "records_considered": len(records),
                    "artworks_fetched": fetched,
                    "published": published,
                    "publish_failed": still_failed,
                    "publish_ambiguous": ambiguous,
                    "runs": [s.model_dump() for s in summaries],
                },
                indent=2,
            )
        )
        return EXIT_OK

    print("=" * 60)
    print(" PINTEREST ART BOT — RETRY")
    print("=" * 60)
    print(f" Mode:                           {'PUBLISH' if publish else 'DRY-RUN'}")
    print(f" Records considered:             {len(records)}")
    print(f" Artworks re-fetched:            {fetched}")
    print(f" Published:                      {published}")
    print(f" Failed again:                   {still_failed}")
    print(f" Ambiguous (needs reconcile):    {ambiguous}")
    print("=" * 60)
    return EXIT_OK


async def _retryable_records(
    app: Application,
    *,
    limit: int,
    state: str,
) -> list[PublicationRecord]:
    """Collect records eligible for another attempt, oldest first."""
    wanted = RETRYABLE_PUBLICATION_STATES
    if state != "all":
        wanted = frozenset({PublicationState(state)})

    records: list[PublicationRecord] = []
    for candidate in wanted:
        records.extend(await app.state_repo.get_records_by_state(candidate))
    return records[:limit]


def run_state(app: Application, args: argparse.Namespace) -> int:
    """Inspect publication state."""
    if args.action == "counts":
        counts = asyncio.run(_state_counts(app))
        if args.json:
            print(json.dumps(counts, indent=2))
            return EXIT_OK
        print("=" * 60)
        print(" PINTEREST ART BOT — STATE COUNTS")
        print("=" * 60)
        for state_name, count in counts.items():
            print(f" {state_name:<20} {count}")
        print("=" * 60)
        return EXIT_OK

    if args.action == "runs":
        runs = asyncio.run(app.state_repo.list_runs(limit=args.limit))
        if args.json:
            print(json.dumps(runs, indent=2))
            return EXIT_OK

        print("=" * 60)
        print(" PINTEREST ART BOT — RUN HISTORY")
        print("=" * 60)
        if not runs:
            print(" No runs recorded yet.")
        for run in runs:
            mode = "DRY-RUN" if run.get("dry_run", True) else "PUBLISH"
            print(
                f" {str(run.get('run_id'))[:18]:18} {mode:8} {run.get('museum')!s:11} "
                f"fetched={run.get('fetched', 0):<4} published={run.get('published', 0):<4} "
                f"errors={run.get('errors', 0):<3} {str(run.get('finished_at'))[:19]}"
            )
            if run.get("error"):
                print(f"     error: {run['error']}")
        print("=" * 60)
        return EXIT_OK

    if args.action == "dry-runs":
        plans = asyncio.run(app.state_repo.list_dry_run_plans(limit=args.limit))
        payload = [
            {
                "artwork_id": plan.get("artwork_id"),
                "title": plan.get("title"),
                "artist": plan.get("artist"),
                "movement": plan.get("movement"),
                "movement_confidence": plan.get("movement_confidence"),
                "board_name": plan.get("board_name"),
                "board_id": plan.get("board_id"),
                "rights_status": plan.get("rights_status"),
                "publishable": plan.get("publishable"),
            }
            for plan in plans
        ]
        if args.json:
            print(json.dumps(payload, indent=2))
            return EXIT_OK

        print("=" * 60)
        print(" PINTEREST ART BOT — DRY-RUN PLANS")
        print("=" * 60)
        for row in payload:
            confidence = row.get("movement_confidence") or 0.0
            print(
                f" {row['artwork_id']!s:<16} {str(row['title'])[:26]:26} "
                f"{row['movement']!s:<18} {confidence:.2f} → {row['board_name']}"
            )
        print("=" * 60)
        return EXIT_OK

    states = [PublicationState.AMBIGUOUS] if args.action == "ambiguous" else None
    records = asyncio.run(app.state_repo.list_records(states=states, limit=args.limit))

    payload = [
        {
            "artwork_id": record.artwork_canonical_id,
            "state": record.state.value,
            "run_id": record.run_id,
            "pin_id": record.pin_id,
            "board": record.board_target.board_key if record.board_target else None,
            "published_at": record.published_at.isoformat() if record.published_at else None,
            "error": record.error_message,
        }
        for record in records
    ]

    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    print("=" * 60)
    print(f" PINTEREST ART BOT — STATE ({args.action.upper()})")
    print("=" * 60)
    for row in payload:
        print(f" {row['artwork_id']:<24} {row['state']:<14} pin={row['pin_id'] or '-'}")
    print("=" * 60)
    return EXIT_OK


def run_schedule(app: Application, args: argparse.Namespace) -> int:
    """Run the gated scheduler loop."""
    if not app.settings.can_schedule:
        sys.stderr.write(
            "ERROR: Scheduled runs are disabled (PINTEREST_PRODUCTION_SCHEDULE_ENABLED=false).\n"
        )
        return EXIT_BLOCKED

    if args.iterations is not None:
        app.settings.scheduler_max_iterations = args.iterations

    scheduler = Scheduler(
        app.runner,
        app.state_repo,
        app.settings,
        museum=args.museum or app.settings.default_museum,
    )

    try:
        results = asyncio.run(scheduler.run_forever())
    except ScheduleDisabledError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return EXIT_BLOCKED
    except LockUnavailableError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return EXIT_BLOCKED
    except KeyboardInterrupt:
        print("Interrupted.")
        return EXIT_OK
    finally:
        scheduler.release()

    for result in results:
        print(
            f"iteration {result.iteration}: published={result.published} "
            f"ambiguous={result.ambiguous} errors={result.errors} "
            f"writes={result.pinterest_writes}"
        )
    return EXIT_OK


def load_config(app: Application) -> Any:
    """Return the application's board configuration model."""
    return app.boards_config


async def _state_counts(app: Application) -> dict[str, int]:
    """Count publication records per state, plus stored dry-run plans."""
    counts: dict[str, int] = {}
    for state in PublicationState:
        records = await app.state_repo.get_records_by_state(state)
        if records:
            counts[state.value] = len(records)
    dry_runs = await app.state_repo.list_dry_run_plans(limit=10_000)
    if dry_runs:
        counts["dry_run_plans"] = len(dry_runs)
    return counts


# ── Entrypoint ───────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the CLI."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    settings = get_settings()
    _apply_logging(settings, args)

    try:
        app = build_application(settings)
    except PinterestError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return EXIT_ERROR

    try:
        if args.command == "preflight":
            return run_preflight(app, args)
        if args.command == "status":
            return run_status(app, args)
        if args.command == "ingest":
            return run_ingest(app, args)
        if args.command == "boards-sync":
            return run_boards_sync(app, args)
        if args.command == "reconcile":
            return run_reconcile(app, args)
        if args.command == "retry":
            return run_retry(app, args)
        if args.command == "state":
            return run_state(app, args)
        if args.command == "schedule":
            return run_schedule(app, args)
    except PinterestError as e:
        sys.stderr.write(f"PINTEREST ERROR: {e}\n")
        return EXIT_ERROR
    except ValueError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return EXIT_USAGE
    finally:
        app.close()

    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
