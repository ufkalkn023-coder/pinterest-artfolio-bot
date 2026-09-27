"""Pipeline orchestrator: ingestion → gates → routing → publication.

Gate order is fixed and each gate can only stop an artwork, never accelerate it:

    1. Rights evaluation        (HARD GATE — default deny)
    2. Media download + validate (streaming, size ceiling, fingerprints)
    3. Deduplication            (published history, exact + near-duplicate media)
    4. Movement classification  (HIGH confidence only may proceed)
    5. Curation eligibility     (rights + classification + media completeness)
    6. Board routing            (deterministic, config driven)
    7. Reservation              (concurrency guard, publish mode only)
    8. Publication              (publish mode only, budgeted, never blind-retried)

SAFETY INVARIANTS enforced in this module:
    - Dry-run mode never writes to Pinterest and never mutates publication
      history. ``summary.pinterest_writes`` is 0 in dry-run.
    - An AMBIGUOUS write is never retried here; it is parked for reconciliation.
    - Publishing requires the master switch, a resolved board ID, and budget.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog

from pinterest_art_bot.boards.config import BoardsConfiguration, default_boards_config
from pinterest_art_bot.boards.router import BoardRouter, RoutingStatus
from pinterest_art_bot.classification.classifier import MovementClassifier
from pinterest_art_bot.config.settings import Settings, build_settings
from pinterest_art_bot.curation.eligibility import EligibilityEvaluator
from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.movement import ConfidenceLevel, MovementClassification
from pinterest_art_bot.domain.publication import (
    RETRYABLE_PUBLICATION_STATES,
    BoardTarget,
    PinMetadata,
    PublicationState,
)
from pinterest_art_bot.domain.work_identity import WorkIdentity, is_same_work, work_identity
from pinterest_art_bot.media.hashing import hamming_distance, is_degenerate
from pinterest_art_bot.media.preparation import ImagePreparer, MediaMode
from pinterest_art_bot.media.validation import MediaValidator
from pinterest_art_bot.museums.base import MuseumProvider, SearchQuery
from pinterest_art_bot.museums.factory import build_registry
from pinterest_art_bot.museums.registry import MuseumRegistry
from pinterest_art_bot.pinterest.boards_sync import normalize_board_name
from pinterest_art_bot.pinterest.client import PinterestClient
from pinterest_art_bot.pinterest.errors import (
    PinterestError,
    ProductionWriteDisabledError,
)
from pinterest_art_bot.pipeline.models import (
    DiscoveryMode,
    DiscoveryRequest,
    DryRunPlan,
    PipelineDecision,
    PipelineItemResult,
    PipelineRunSummary,
)
from pinterest_art_bot.pipeline.pin_composer import PinComposer
from pinterest_art_bot.pipeline.publication import PublicationService
from pinterest_art_bot.rights.evaluator import RightsEvaluator
from pinterest_art_bot.state.factory import create_state_repository
from pinterest_art_bot.state.repository import StateRepository

logger = structlog.get_logger()


@dataclass(frozen=True)
class DiscoveryResult:
    """Candidates found for a run, plus where the next run should resume."""

    artworks: list[Artwork]
    next_offset: int | None = None


class PipelineRunner:
    """Orchestrates ingestion, rights checks, media validation, classification, and publishing."""

    def __init__(
        self,
        *,
        registry: MuseumRegistry | None = None,
        providers: dict[str, MuseumProvider] | None = None,
        rights_evaluator: RightsEvaluator | None = None,
        media_validator: MediaValidator | None = None,
        classifier: MovementClassifier | None = None,
        router: BoardRouter | None = None,
        boards_config: BoardsConfiguration | None = None,
        curator: EligibilityEvaluator | None = None,
        pin_composer: PinComposer | None = None,
        image_preparer: ImagePreparer | None = None,
        state_repo: StateRepository | None = None,
        pinterest_client: PinterestClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or build_settings(load_env_file=False)
        self.rights_evaluator = rights_evaluator or RightsEvaluator(
            enforce_strict=self.settings.rights_enforce_strict,
            auto_publishable=self.settings.auto_publishable_rights_statuses,
        )
        self.media_validator = media_validator or MediaValidator()
        self.classifier = classifier or MovementClassifier(
            auto_threshold=self.settings.movement_confidence_auto_threshold,
            minimum_threshold=self.settings.movement_confidence_minimum,
        )
        self.boards_config = boards_config or default_boards_config()
        self.router = router or BoardRouter(
            self.boards_config,
            allow_medium_confidence=self.settings.allow_medium_confidence_routing,
        )
        self.curator = curator or EligibilityEvaluator(self.media_validator)
        self.pin_composer = pin_composer or PinComposer(
            title_max_length=self.settings.pin_title_max_length,
            description_max_length=self.settings.pin_description_max_length,
            alt_text_max_length=self.settings.pin_alt_text_max_length,
        )
        self.state_repo = state_repo or create_state_repository(self.settings)
        self.pinterest_client = pinterest_client

        if providers is not None:
            self.providers: dict[str, MuseumProvider] = dict(providers)
        else:
            source_registry = registry or build_registry(self.settings)
            self.providers = {
                provider.source_id: provider for provider in source_registry.get_all()
            }

        self.image_preparer = image_preparer or (
            ImagePreparer(
                max_width=self.settings.image_max_width,
                jpeg_quality=self.settings.image_jpeg_quality,
                mode=MediaMode(self.settings.pin_media_mode),
            )
            if self.settings.image_prepare_enabled
            else None
        )
        self.publisher = (
            PublicationService(
                client=pinterest_client,
                state_repo=self.state_repo,
                boards_config=self.boards_config,
                per_board_daily_limit=self.settings.max_pins_per_board_per_day,
                preparer=self.image_preparer,
            )
            if pinterest_client is not None
            else None
        )

        # A runner owns per-run mutable state, so only one run may be in flight.
        # Concurrent callers wait rather than corrupting budgets and counters.
        self._run_lock = asyncio.Lock()
        self._pins_reserved_this_run = 0
        self._known_perceptual_hashes: list[tuple[str, str]] = []
        self._known_work_identities: list[tuple[str, WorkIdentity]] = []
        self._work_identity_owner: dict[str, str] = {}
        self._active_summary: PipelineRunSummary | None = None
        self._remote_board_ids: dict[str, str] = {}

    # ── Entry point ──────────────────────────────────────────────────
    async def run(
        self,
        request: DiscoveryRequest,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> PipelineRunSummary:
        """Execute an ingestion run, publishing when the request and settings allow it."""
        async with self._run_lock:
            return await self._run_locked(request, http_client=http_client)

    async def _run_locked(
        self,
        request: DiscoveryRequest,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> PipelineRunSummary:
        """Run the pipeline, assuming exclusive access to this runner."""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = datetime.now(UTC)
        publish = request.publish

        if publish and not self.settings.can_publish:
            raise ProductionWriteDisabledError(
                "Publishing was requested but PUBLISH_TO_PINTEREST is not enabled "
                "(or PINTEREST_ACCESS_TOKEN is missing)."
            )
        if publish and self.pinterest_client is None:
            raise ValueError("Publishing requires a Pinterest client; none was provided.")

        self._pins_reserved_this_run = 0
        if self.publisher is not None:
            self.publisher.reset_attempts()
        self._known_perceptual_hashes = await self.state_repo.list_perceptual_hashes()
        self._known_work_identities = []
        self._work_identity_owner = {}
        for work_key, artwork_id, identity in await self.state_repo.list_work_identities():
            self._known_work_identities.append((work_key, identity))
            self._work_identity_owner.setdefault(work_key, artwork_id)
        self._remote_board_ids = {}

        logger.info(
            "pipeline_run_started",
            run_id=run_id,
            museum=request.museum,
            limit=request.limit,
            mode=request.mode.value,
            publish=publish,
        )

        summary = PipelineRunSummary(run_id=run_id, museum=request.museum, dry_run=not publish)
        self._active_summary = summary

        provider = self.providers.get(request.museum)
        if not provider:
            raise ValueError(
                f"Unknown museum provider: '{request.museum}'. "
                f"Supported: {list(self.providers.keys())}"
            )

        # ── 1. Museum Artwork Discovery ──────────────────────────────────
        # Successive runs must explore new ground: start where the last run stopped
        # unless the caller pinned an explicit offset.
        cursor_name = f"discovery:{request.museum}"
        resume_from = request.offset
        if not request.offset and request.rotate:
            cursor_value = await self.state_repo.get_cursor(cursor_name)
            resume_from = int(cursor_value or 0)

        try:
            discovery = await self._fetch_candidates(provider, request, offset=resume_from)
        except Exception as e:
            logger.error("pipeline_discovery_failed", museum=request.museum, error=str(e))
            await self._record_run(
                run_id,
                request,
                PipelineRunSummary(run_id=run_id, museum=request.museum, dry_run=not publish),
                started_at,
                error=str(e),
            )
            raise

        candidates = discovery.artworks
        # An exhausted listing wraps back to the start of the collection.
        await self.state_repo.set_cursor(cursor_name, discovery.next_offset or 0)

        summary.fetched = len(candidates)
        logger.info(
            "pipeline_candidates_fetched",
            count=len(candidates),
            run_id=run_id,
            started_at=resume_from,
            next_offset=discovery.next_offset,
        )

        # ── 2. Housekeeping and publication preparation ───────────────────
        # Retention runs in every mode: abandoned reservations and rejected
        # fingerprints would otherwise accumulate forever on a dry-run-only host.
        purged = await self._run_housekeeping()
        logger.info("state_housekeeping_completed", **purged)

        pin_budget = 0
        if publish:
            pin_budget = await self._resolve_pin_budget()
            await self._load_board_activity()
            await self._load_remote_board_ids()
            logger.info(
                "publish_prepared",
                run_id=run_id,
                pin_budget=pin_budget,
                known_perceptual_hashes=len(self._known_perceptual_hashes),
                known_work_identities=len(self._known_work_identities),
                remote_boards=len(self._remote_board_ids),
            )

        # ── 3. Process Candidates Through Pipeline Gates ─────────────────
        if publish and self.settings.board_round_robin and self.publisher is not None:
            candidates = self._spread_across_boards(candidates)

        owns_http = http_client is None
        client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0),
            follow_redirects=True,
            headers={"User-Agent": self.settings.museum_user_agent},
        )

        semaphore = asyncio.Semaphore(self.settings.pipeline_max_concurrency)

        async def process(artwork: Artwork) -> PipelineItemResult:
            async with semaphore:
                return await self._process_single_artwork(
                    artwork=artwork,
                    run_id=run_id,
                    summary=summary,
                    http_client=client,
                    publish=publish,
                    pin_budget=pin_budget,
                )

        try:
            results = await asyncio.gather(
                *(process(artwork) for artwork in candidates),
                return_exceptions=True,
            )
        finally:
            if owns_http:
                await client.aclose()

        for artwork, result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                summary.errors += 1
                item = PipelineItemResult(
                    canonical_id=artwork.canonical_id,
                    title=artwork.title,
                    decision=PipelineDecision.ERROR,
                    reason=f"Unhandled processing error: {result}",
                    error=str(result),
                )
                logger.error(
                    "pipeline_item_crashed",
                    canonical_id=artwork.canonical_id,
                    error=str(result),
                )
            else:
                item = result
            summary.items.append(item)
            if item.plan:
                summary.plans.append(item.plan)

        await self._record_run(run_id, request, summary, started_at, error=None)

        logger.info(
            "pipeline_run_completed",
            run_id=run_id,
            fetched=summary.fetched,
            ready=summary.ready_dry_run_plans,
            published=summary.published,
            ambiguous=summary.publish_ambiguous,
            held=summary.held_for_review,
            errors=summary.errors,
            writes=summary.pinterest_writes,
        )

        return summary

    async def _record_run(
        self,
        run_id: str,
        request: DiscoveryRequest,
        summary: PipelineRunSummary,
        started_at: datetime,
        *,
        error: str | None = None,
    ) -> None:
        """Persist the run so the outcome stays auditable after the process exits.

        Never allowed to break a run: history is a convenience, not a gate.
        """
        payload = summary.model_dump(exclude={"items", "plans"})
        payload["mode"] = request.mode.value
        payload["publish_requested"] = request.publish
        payload["limit"] = request.limit
        payload["started_at"] = started_at.isoformat()
        payload["finished_at"] = datetime.now(UTC).isoformat()
        payload["error"] = error

        try:
            await self.state_repo.save_run(run_id, payload)
        except Exception as e:
            logger.warning("run_history_write_failed", run_id=run_id, error=str(e))

    # ── Discovery ────────────────────────────────────────────────────
    async def _fetch_candidates(
        self,
        provider: MuseumProvider,
        request: DiscoveryRequest,
        *,
        offset: int = 0,
    ) -> DiscoveryResult:
        """Fetch candidates according to discovery mode, paging until the limit is met."""
        if request.mode == DiscoveryMode.OBJECT_ID:
            artworks: list[Artwork] = []
            for obj_id in request.object_ids:
                art = await provider.get_artwork(obj_id)
                if art:
                    artworks.append(art)
            return DiscoveryResult(artworks=self._deduplicate_candidates(artworks))

        collected: list[Artwork] = []
        current = offset
        next_offset: int | None = offset
        for _ in range(request.max_pages):
            remaining = request.limit - len(collected)
            if remaining <= 0:
                break

            if request.mode == DiscoveryMode.SEARCH and request.query:
                page = await provider.search(
                    SearchQuery(
                        query=request.query,
                        page_size=min(request.page_size, remaining),
                        public_domain_only=request.public_domain_only,
                        has_images=request.require_image,
                        offset=current,
                    )
                )
            else:
                page = await provider.discover(
                    page_size=min(request.page_size, remaining),
                    offset=current,
                )

            collected.extend(page.artworks)
            if not page.has_more or not page.artworks:
                # An exhausted listing means there is nothing left to resume from.
                if page.has_more and not page.artworks:
                    next_offset = current + request.page_size
                else:
                    next_offset = None
                break

            next_offset = (
                page.next_offset if page.next_offset is not None else current + len(page.artworks)
            )
            current = next_offset

        return DiscoveryResult(
            artworks=self._deduplicate_candidates(collected)[: request.limit],
            next_offset=next_offset,
        )

    @staticmethod
    def _deduplicate_candidates(artworks: list[Artwork]) -> list[Artwork]:
        """Drop repeated canonical IDs, preserving discovery order.

        Processing the same canonical ID twice in one run would risk a double write.
        """
        seen: set[str] = set()
        unique: list[Artwork] = []
        for artwork in artworks:
            if artwork.canonical_id in seen:
                continue
            seen.add(artwork.canonical_id)
            unique.append(artwork)
        return unique

    def _spread_across_boards(self, candidates: list[Artwork]) -> list[Artwork]:
        """Order candidates so a run spreads its Pins across boards.

        Artworks are bucketed by the board their classification routes to, and
        the least-used boards are processed first. The order within a board keeps
        the museum's ranking.
        """
        assert self.publisher is not None
        buckets: dict[str, list[Artwork]] = {}
        for artwork in candidates:
            key = self._predicted_board_key(artwork)
            buckets.setdefault(key, []).append(artwork)

        ordered: list[Artwork] = []
        for key in sorted(buckets, key=self.publisher.board_rank):
            ordered.extend(buckets[key])
        return ordered

    def _predicted_board_key(self, artwork: Artwork) -> str:
        """Board this artwork would route to, for ordering purposes only."""
        classification = self.classifier.classify(artwork)
        if classification.confidence_level != ConfidenceLevel.HIGH:
            return "~unrouted"
        routing = self.router.route(classification)
        return routing.target.board_key if routing.target else "~unrouted"

    async def _run_housekeeping(self) -> dict[str, int]:
        """Apply retention policies. Never fatal: housekeeping is best-effort."""
        removed = {
            "expired_reservations": 0,
            "old_dry_run_plans": 0,
            "old_runs": 0,
            "rejected_fingerprints": 0,
        }
        repo = self.state_repo
        try:
            # Recover writes abandoned by a crashed process, otherwise the record
            # would stay in-flight forever and be skipped by every future run.
            swept = await repo.sweep_stale_inflight_records(
                self.settings.state_inflight_timeout_seconds
            )
            if swept:
                logger.warning(
                    "stale_inflight_records_recovered",
                    count=len(swept),
                    artwork_ids=swept[:10],
                    timeout_seconds=self.settings.state_inflight_timeout_seconds,
                )
            removed["expired_reservations"] = await repo.purge_expired_reservations(
                self.settings.state_reservation_ttl_seconds
            )
            removed["old_dry_run_plans"] = await repo.purge_dry_run_plans(
                self.settings.state_dry_run_retention_seconds
            )
            removed["old_runs"] = await repo.purge_run_history(
                self.settings.state_run_retention_seconds
            )
            removed["rejected_fingerprints"] = await repo.purge_rejected_fingerprints(
                self.settings.state_fingerprint_retention_seconds
            )
        except Exception as e:
            logger.warning("state_housekeeping_failed", error=str(e))
        return removed

    # ── Publication preparation ──────────────────────────────────────
    async def _resolve_pin_budget(self) -> int:
        """How many Pins this run may write, respecting run and daily caps."""
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        published_today = await self.state_repo.count_published_since(midnight)
        daily_remaining = max(0, self.settings.pinterest_max_pins_per_day - published_today)
        return min(self.settings.effective_max_pins_per_run, daily_remaining)

    async def _load_board_activity(self) -> None:
        """Load today's per-board usage so budgets and rotation are accurate."""
        if self.publisher is None:
            return
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        published_per_board = await self.state_repo.count_published_per_board_since(day_start)
        last_published = await self.state_repo.last_published_at_per_board()
        last_used = {
            board_key: published_at.isoformat()
            for board_key, published_at in last_published.items()
        }
        self.publisher.load_board_activity(published_per_board, last_used)
        logger.info(
            "board_activity_loaded",
            boards_used=len(published_per_board),
            per_board_limit=self.settings.max_pins_per_board_per_day,
        )

    async def _load_remote_board_ids(self) -> None:
        """Read (never write) live Pinterest boards to resolve IDs by name."""
        if self.pinterest_client is None:
            return
        try:
            boards = await self.pinterest_client.list_boards()
        except PinterestError as e:
            logger.warning("remote_board_lookup_failed", error=str(e))
            return

        for board in boards:
            self._remote_board_ids[normalize_board_name(board.name)] = board.id
        if self.publisher is not None:
            self.publisher.set_remote_board_ids(self._remote_board_ids)
        logger.info("remote_boards_loaded", count=len(self._remote_board_ids))

    def _resolve_board_id(self, target: BoardTarget) -> str | None:
        """Resolve a board ID from local config, then from the live board list."""
        if target.board_id:
            return target.board_id

        board_config = self.boards_config.get_board_by_key(target.board_key)
        if board_config and board_config.board_id:
            return board_config.board_id

        return self._remote_board_ids.get(normalize_board_name(target.board_name))

    # ── Per-artwork processing ───────────────────────────────────────
    async def _process_single_artwork(
        self,
        artwork: Artwork,
        run_id: str,
        summary: PipelineRunSummary,
        http_client: httpx.AsyncClient,
        *,
        publish: bool,
        pin_budget: int,
    ) -> PipelineItemResult:
        """Process a single artwork through all gates."""
        canonical_id = artwork.canonical_id
        title = artwork.title

        try:
            # ── Gate 1: Rights Verification ──────────────────────────────
            rights_eval = self.rights_evaluator.evaluate(artwork)
            if not rights_eval.is_publishable:
                # Permissive-but-excluded is reported separately from a hard deny,
                # so operators can see how much of a catalogue the policy blocks.
                decision = (
                    PipelineDecision.RIGHTS_REVIEW_REQUIRED
                    if rights_eval.requires_review
                    else PipelineDecision.RIGHTS_DENIED
                )
                if rights_eval.requires_review:
                    summary.rights_review_required += 1
                else:
                    summary.rights_rejected += 1
                logger.info(
                    "gate_rejected_rights",
                    canonical_id=canonical_id,
                    status=rights_eval.status.value,
                    requires_review=rights_eval.requires_review,
                    reason=rights_eval.reason,
                )
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=decision,
                    reason=f"Rights rejected: {rights_eval.reason or rights_eval.status.value}",
                    raw_details={
                        "rights_status": rights_eval.status.value,
                        "requires_review": rights_eval.requires_review,
                    },
                )
            summary.rights_approved += 1

            # ── Gate 2: Media URL & Image Validation ─────────────────────
            image_url = artwork.best_image_url
            if not image_url:
                summary.image_invalid += 1
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=PipelineDecision.NO_IMAGE,
                    reason="No image URL available for artwork.",
                )

            needs_bytes = (
                self.image_preparer is not None and self.image_preparer.mode == MediaMode.BASE64
            )
            media_result = await self.media_validator.download_and_validate(
                image_url, client=http_client, keep_bytes=needs_bytes
            )
            if not media_result.is_valid:
                summary.image_invalid += 1
                issues_str = "; ".join(media_result.issues)
                logger.info("gate_rejected_media", canonical_id=canonical_id, issues=issues_str)
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=PipelineDecision.IMAGE_INVALID,
                    reason=f"Media validation failed: {issues_str}",
                )

            summary.image_valid += 1
            image_sha256 = media_result.sha256 or ""
            perceptual_hash = media_result.perceptual_hash

            # ── Gate 3: State Deduplication ──────────────────────────────
            dedup = await self._check_duplicates(artwork, image_sha256, perceptual_hash, summary)
            if dedup is not None:
                return dedup

            existing_record = await self.state_repo.get_record(canonical_id)

            # ── Gate 4: Movement Classification ──────────────────────────
            classification = self.classifier.classify(artwork)
            movement_name = classification.primary_movement.value
            confidence = classification.confidence

            # INVARIANT: Only HIGH confidence classification can auto-route
            if classification.confidence_level != ConfidenceLevel.HIGH:
                summary.held_for_review += 1
                logger.info(
                    "gate_held_classification",
                    canonical_id=canonical_id,
                    level=classification.confidence_level.value,
                    confidence=confidence,
                    reason=classification.reason,
                )
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=PipelineDecision.CLASSIFICATION_UNCERTAIN,
                    reason=(
                        f"Classification confidence too low ({confidence:.2f}, "
                        f"level={classification.confidence_level.value}) "
                        f"for movement {movement_name}. Held for review."
                    ),
                    movement=movement_name,
                    confidence=confidence,
                    image_sha256=image_sha256,
                )

            summary.high_confidence_classified += 1

            # ── Gate 5: Curation Eligibility ─────────────────────────────
            curation = self.curator.evaluate(artwork, rights_eval, classification)
            if not curation.eligible:
                summary.held_for_review += 1
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=PipelineDecision.ROUTING_HELD,
                    reason="; ".join(curation.reasons) or "Curation rejected the artwork.",
                    movement=movement_name,
                    confidence=confidence,
                    image_sha256=image_sha256,
                )

            # ── Gate 6: Board Routing ────────────────────────────────────
            routing = self.router.route(classification)
            target: BoardTarget | None = routing.target
            if routing.status == RoutingStatus.ROUTING_HELD or not routing.routed or target is None:
                summary.held_for_review += 1
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=PipelineDecision.ROUTING_HELD,
                    reason=routing.reason,
                    movement=movement_name,
                    confidence=confidence,
                    image_sha256=image_sha256,
                )

            if routing.status == RoutingStatus.NO_BOARD:
                summary.board_id_missing += 1
                return PipelineItemResult(
                    canonical_id=canonical_id,
                    title=title,
                    decision=PipelineDecision.NO_BOARD_MAPPING,
                    reason=routing.reason,
                    movement=movement_name,
                    confidence=confidence,
                    image_sha256=image_sha256,
                )

            summary.board_routed += 1
            target_board: BoardTarget = target

            # ── Gate 7: Plan / Publish ───────────────────────────────────
            pin_meta = self.pin_composer.compose(artwork, movement_name, rights_eval, target_board)

            if not publish:
                return await self._record_dry_run(
                    artwork=artwork,
                    run_id=run_id,
                    summary=summary,
                    movement_name=movement_name,
                    classification=classification,
                    target_board=target_board,
                    pin_meta=pin_meta,
                    rights_status=rights_eval.status.value,
                    rights_reason=rights_eval.reason or "Rights cleared",
                    image_url=image_url,
                    image_sha256=image_sha256,
                )

            assert self.publisher is not None  # guaranteed when publish=True
            result = await self.publisher.publish(
                artwork=artwork,
                run_id=run_id,
                budget=pin_budget,
                rights_eval=rights_eval,
                classification=classification,
                target_board=target_board,
                pin_meta=pin_meta,
                image_url=image_url,
                image_sha256=image_sha256,
                perceptual_hash=perceptual_hash,
                existing_record=existing_record,
                image_bytes=media_result.data,
            )
            self._count_publication_outcome(result)
            if result.decision == PipelineDecision.PUBLISHED:
                await self._remember_work_identity(artwork)
            return result

        except Exception as e:
            logger.error("pipeline_item_failed", canonical_id=canonical_id, error=str(e))
            summary.errors += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=title,
                decision=PipelineDecision.ERROR,
                reason=f"Processing error: {e}",
                error=str(e),
            )

    def _count_publication_outcome(self, result: PipelineItemResult) -> None:
        """Fold a publication outcome into the run counters.

        Kept next to the call site so the summary stays truthful without the
        publication service needing to know about runs.
        """
        summary = self._active_summary
        if summary is None:
            return
        match result.decision:
            case PipelineDecision.PUBLISHED:
                summary.published += 1
                summary.pinterest_writes += 1
            case PipelineDecision.PUBLISH_AMBIGUOUS:
                summary.publish_ambiguous += 1
            case PipelineDecision.PUBLISH_FAILED:
                summary.publish_failed += 1
            case PipelineDecision.ALREADY_RESERVED:
                summary.reserved_conflicts += 1
            case PipelineDecision.BOARD_ID_MISSING:
                summary.board_id_missing += 1
            case PipelineDecision.BOARD_BUDGET_EXHAUSTED:
                summary.board_budget_exhausted += 1
            case PipelineDecision.PIN_BUDGET_EXHAUSTED:
                summary.held_for_review += 1
            case _:
                pass

    # ── Deduplication ────────────────────────────────────────────────
    async def _check_duplicates(
        self,
        artwork: Artwork,
        image_sha256: str,
        perceptual_hash: str | None,
        summary: PipelineRunSummary,
    ) -> PipelineItemResult | None:
        """Return a duplicate result, or None when the artwork may proceed.

        Publication history is authoritative, so the decision depends on the
        recorded state:

        - PUBLISHED   → never publish again
        - AMBIGUOUS   → never retry blindly; reconcile first
        - FAILED      → retryable (a definitive failure left nothing behind)
        - RECONCILED  → retryable (we proved no Pin exists)
        - any other   → an in-flight record exists, so skip this run
        """
        canonical_id = artwork.canonical_id
        record = await self.state_repo.get_record(canonical_id)

        if record is not None and record.state == PublicationState.AMBIGUOUS:
            # INVARIANT: ambiguous writes are never retried from the pipeline.
            summary.publish_ambiguous += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=record.artwork_canonical_id,
                decision=PipelineDecision.PUBLISH_AMBIGUOUS,
                reason=(
                    "A previous Pinterest write for this artwork is AMBIGUOUS. "
                    "Run 'pinterest-art-bot reconcile' before any further attempt."
                ),
            )

        if record is not None and record.state == PublicationState.PUBLISHED:
            summary.duplicates_published += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=record.artwork_canonical_id,
                decision=PipelineDecision.ALREADY_PUBLISHED,
                reason=f"Artwork {canonical_id} was already published.",
                image_sha256=image_sha256,
            )

        if record is not None and record.state in RETRYABLE_PUBLICATION_STATES:
            # A definitive failure or a reconciled record may be attempted again.
            summary.retries_considered += 1
            logger.info(
                "dedup_retryable_state",
                canonical_id=canonical_id,
                state=record.state.value,
                retry_count=record.retry_count,
            )
        elif record is not None:
            summary.duplicates_seen += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=record.artwork_canonical_id,
                decision=PipelineDecision.ALREADY_SEEN,
                reason=(
                    f"Artwork {canonical_id} already has an in-flight record "
                    f"({record.state.value}). Skipped to avoid a concurrent write."
                ),
                image_sha256=image_sha256,
            )

        existing_by_hash = await self.state_repo.check_image_hash(image_sha256)
        if existing_by_hash:
            summary.duplicates_image_hash += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=canonical_id,
                decision=PipelineDecision.DUPLICATE_IMAGE_HASH,
                reason=(
                    "Exact image SHA-256 already recorded for "
                    f"{existing_by_hash.artwork_canonical_id}."
                ),
                image_sha256=image_sha256,
            )

        cross_source = self._find_cross_source_duplicate(artwork, canonical_id)
        if cross_source:
            summary.duplicates_cross_source += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=artwork.title,
                decision=PipelineDecision.DUPLICATE_WORK,
                reason=(
                    f"The same work is already handled as {cross_source} in another "
                    "museum's catalogue."
                ),
                image_sha256=image_sha256,
            )

        near_match = self._find_near_duplicate(perceptual_hash, canonical_id)
        if near_match:
            summary.duplicates_near_match += 1
            return PipelineItemResult(
                canonical_id=canonical_id,
                title=canonical_id,
                decision=PipelineDecision.DUPLICATE_NEAR_MATCH,
                reason=(
                    f"Image is a near-duplicate of {near_match} "
                    f"at the configured perceptual hash distance."
                ),
                image_sha256=image_sha256,
            )

        return None

    def _find_cross_source_duplicate(self, artwork: Artwork, canonical_id: str) -> str | None:
        """Find an earlier record of the same work, possibly from another museum."""
        if not self.settings.cross_source_dedup_enabled:
            return None
        identity = work_identity(artwork)
        if not identity.is_comparable:
            return None

        for known_key, known_identity in self._known_work_identities:
            if known_key == identity.key or is_same_work(identity, known_identity):
                return self._work_identity_owner.get(known_key)
        return None

    async def _remember_work_identity(self, artwork: Artwork) -> None:
        """Record the artwork's cross-source identity for future duplicate checks."""
        if not self.settings.cross_source_dedup_enabled:
            return
        identity = work_identity(artwork)
        if not identity.is_comparable:
            return
        await self.state_repo.record_work_identity(identity.key, artwork.canonical_id, identity)
        self._known_work_identities.append((identity.key, identity))
        self._work_identity_owner.setdefault(identity.key, artwork.canonical_id)

    def _find_near_duplicate(self, perceptual_hash: str | None, canonical_id: str) -> str | None:
        """Find a previously handled artwork whose image looks equivalent."""
        if not perceptual_hash or not self.settings.perceptual_hash_enabled:
            return None
        if is_degenerate(perceptual_hash):
            # A flat image yields an uninformative hash; never trust it for verdicts.
            return None

        threshold = self.settings.perceptual_hash_max_distance
        for known_hash, known_id in self._known_perceptual_hashes:
            if known_id == canonical_id or is_degenerate(known_hash):
                continue
            if hamming_distance(perceptual_hash, known_hash) <= threshold:
                return known_id
        return None

    # ── Dry run ──────────────────────────────────────────────────────
    async def _record_dry_run(
        self,
        *,
        artwork: Artwork,
        run_id: str,
        summary: PipelineRunSummary,
        movement_name: str,
        classification: MovementClassification,
        target_board: BoardTarget,
        pin_meta: PinMetadata,
        rights_status: str,
        rights_reason: str,
        image_url: str,
        image_sha256: str,
    ) -> PipelineItemResult:
        """Persist a plan in dry-run storage without touching publication history."""
        canonical_id = artwork.canonical_id
        confidence = classification.confidence

        plan = DryRunPlan(
            artwork_id=canonical_id,
            title=artwork.title,
            artist=artwork.artist_name,
            movement=movement_name,
            movement_confidence=confidence,
            confidence_level=classification.confidence_level.value,
            board_key=target_board.board_key,
            board_name=target_board.board_name,
            board_id=target_board.board_id,  # None until synced with Pinterest
            image_url=image_url,
            image_sha256=image_sha256,
            museum_url=artwork.museum_object_url,
            rights_status=rights_status,
            rights_reason=rights_reason,
            publishable=False,  # Dry run is strictly unpublishable
            dry_run=True,
            pin_metadata=pin_meta,
        )

        # NOTE: no work identity is recorded here on purpose. A dry run must not
        # change what a later run reports as a duplicate.
        await self.state_repo.record_dry_run_plan(
            artwork_id=canonical_id,
            run_id=run_id,
            plan_data=plan.model_dump(),
        )

        summary.ready_dry_run_plans += 1

        return PipelineItemResult(
            canonical_id=canonical_id,
            title=artwork.title,
            decision=PipelineDecision.READY,
            reason="Artwork passed all gates and generated a dry-run plan.",
            movement=movement_name,
            confidence=confidence,
            board_key=target_board.board_key,
            image_sha256=image_sha256,
            plan=plan,
        )

    # ── Publication ──────────────────────────────────────────────────
