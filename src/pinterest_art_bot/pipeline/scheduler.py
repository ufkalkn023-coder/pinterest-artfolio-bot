"""Production scheduler.

Runs the pipeline on a fixed interval. Gated by
`PINTEREST_PRODUCTION_SCHEDULE_ENABLED`, which defaults to False — the scheduler
refuses to start unless the operator explicitly enables it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.pipeline.lock import ProcessLock
from pinterest_art_bot.pipeline.models import DiscoveryRequest
from pinterest_art_bot.pipeline.runner import PipelineRunner
from pinterest_art_bot.state.repository import StateRepository

logger = structlog.get_logger()


class ScheduleDisabledError(RuntimeError):
    """Raised when scheduling is attempted while the safety switch is off."""


@dataclass
class ScheduledRunResult:
    """Outcome of a single scheduled iteration."""

    iteration: int
    museum: str
    published: int
    ambiguous: int
    errors: int
    pinterest_writes: int


class Scheduler:
    """Interval-driven pipeline runner."""

    def __init__(
        self,
        runner: PipelineRunner,
        state_repo: StateRepository,
        settings: Settings,
        *,
        museum: str | None = None,
        lock: ProcessLock | None = None,
    ) -> None:
        self._runner = runner
        self._state_repo = state_repo
        self._settings = settings
        self._museum = museum or settings.default_museum
        self._stop = asyncio.Event()
        self._lock = lock or ProcessLock(Path(settings.scheduler_lock_path))

    def request_stop(self) -> None:
        """Ask the loop to finish the current iteration and exit."""
        self._stop.set()

    async def run_forever(self) -> list[ScheduledRunResult]:
        """Run until stopped, or until the configured iteration cap is reached."""
        if not self._settings.can_schedule:
            raise ScheduleDisabledError(
                "Scheduled runs are disabled (PINTEREST_PRODUCTION_SCHEDULE_ENABLED=false)."
            )

        # One scheduler at a time: a second loop would double API traffic.
        self._lock.acquire()

        interval = self._settings.scheduler_interval_seconds
        max_iterations = self._settings.scheduler_max_iterations
        results: list[ScheduledRunResult] = []
        iteration = 0

        logger.info(
            "scheduler_started",
            museum=self._museum,
            interval_seconds=interval,
            max_iterations=max_iterations,
        )

        while not self._stop.is_set():
            iteration += 1
            result = await self.run_once(iteration)
            results.append(result)

            if max_iterations and iteration >= max_iterations:
                logger.info("scheduler_iteration_cap_reached", iterations=iteration)
                break
            if self._stop.is_set():
                break

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

        logger.info("scheduler_stopped", iterations=iteration)
        return results

    def release(self) -> None:
        """Release the single-instance lock."""
        self._lock.release()

    async def run_once(self, iteration: int = 1) -> ScheduledRunResult:
        """Execute one scheduled pipeline iteration with full error isolation."""
        request = DiscoveryRequest(
            museum=self._museum,
            limit=self._settings.default_museum_limit,
            publish=self._settings.can_publish,
            max_pages=self._settings.pipeline_max_pages,
        )

        try:
            summary = await self._runner.run(request)
        except Exception as e:
            logger.error("scheduled_run_failed", iteration=iteration, error=str(e))
            return ScheduledRunResult(
                iteration=iteration,
                museum=self._museum,
                published=0,
                ambiguous=0,
                errors=1,
                pinterest_writes=0,
            )

        # Reconcile before the next iteration so ambiguous writes never accumulate.
        if summary.publish_ambiguous:
            logger.warning(
                "scheduled_run_has_ambiguous_writes",
                iteration=iteration,
                ambiguous=summary.publish_ambiguous,
                published=summary.published,
                action="reconciling automatically",
            )
            await self._reconcile()

        return ScheduledRunResult(
            iteration=iteration,
            museum=self._museum,
            published=summary.published,
            ambiguous=summary.publish_ambiguous,
            errors=summary.errors,
            pinterest_writes=summary.pinterest_writes,
        )

    async def _reconcile(self) -> None:
        """Resolve ambiguous writes from the previous iteration."""
        from pinterest_art_bot.pinterest.reconciliation import AmbiguousWriteReconciler

        client = self._runner.pinterest_client
        if client is None:
            return
        reconciler = AmbiguousWriteReconciler(client, self._state_repo)
        summary = await reconciler.reconcile_all()
        logger.info(
            "scheduled_reconciliation_completed",
            published=summary.published,
            reconciled=summary.reconciled,
            manual_review_required=summary.manual_review_required,
        )
