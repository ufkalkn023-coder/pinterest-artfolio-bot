"""Tests for settings, the application graph, and the scheduler gate."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from pinterest_art_bot.bootstrap import (
    Application,
    build_application,
    build_pinterest_client,
    require_pinterest_client,
)
from pinterest_art_bot.config.settings import Settings, build_settings
from pinterest_art_bot.media.validation import MediaValidator
from pinterest_art_bot.pinterest.client import SafePinterestClient, StubPinterestClient
from pinterest_art_bot.pinterest.errors import PinterestAuthError
from pinterest_art_bot.pipeline.models import DiscoveryRequest
from pinterest_art_bot.pipeline.scheduler import ScheduleDisabledError, Scheduler
from pinterest_art_bot.state.sqlite import SqliteStateRepository


def settings(tmp_path: Path, **overrides) -> Settings:
    """Isolated settings pointing at a temporary state database."""
    base = {
        "_env_file": None,
        "state_db_path": str(tmp_path / "state.db"),
        "boards_config_path": str(Path("config/boards.yaml")),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestSettingsDefaults:
    def test_all_production_switches_off(self):
        config = Settings(_env_file=None)  # type: ignore[call-arg]
        assert config.publish_to_pinterest is False
        assert config.pinterest_auto_create_boards is False
        assert config.pinterest_production_schedule_enabled is False
        assert config.rights_enforce_strict is True
        assert config.can_publish is False
        assert config.can_schedule is False

    def test_build_settings_can_skip_env_file(self):
        assert build_settings(load_env_file=False).can_publish is False

    def test_effective_budget_never_exceeds_daily_cap(self):
        config = Settings(  # type: ignore[call-arg]
            _env_file=None, pinterest_max_pins_per_run=100, pinterest_max_pins_per_day=10
        )
        assert config.effective_max_pins_per_run == 10

    def test_paths_resolve(self):
        config = Settings(  # type: ignore[call-arg]
            _env_file=None, state_db_path="data/x.db", boards_config_path="config/y.yaml"
        )
        assert config.state_db_file.name == "x.db"
        assert config.boards_config_file.name == "y.yaml"

    def test_token_whitespace_is_not_a_credential(self):
        config = Settings(_env_file=None, pinterest_access_token="   ")  # type: ignore[call-arg]
        assert config.is_pinterest_configured is False

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, movement_confidence_auto_threshold=1.5)  # type: ignore[call-arg]

    def test_invalid_concurrency_rejected(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, pipeline_max_concurrency=0)  # type: ignore[call-arg]


class TestPinterestClientConstruction:
    def test_no_client_without_token(self):
        assert build_pinterest_client(Settings(_env_file=None)) is None  # type: ignore[call-arg]

    def test_require_client_raises_without_token(self):
        with pytest.raises(PinterestAuthError, match="PINTEREST_ACCESS_TOKEN"):
            require_pinterest_client(Settings(_env_file=None))  # type: ignore[call-arg]

    def test_client_is_always_wrapped(self, tmp_path):
        config = settings(tmp_path, publish_to_pinterest=True, pinterest_access_token="tok")
        client = build_pinterest_client(config)
        assert isinstance(client, SafePinterestClient)
        assert isinstance(client.inner, object)
        assert not isinstance(client.inner, StubPinterestClient)


class TestApplicationGraph:
    def test_builds_without_credentials(self, tmp_path):
        app = build_application(settings(tmp_path))
        assert isinstance(app, Application)
        assert app.pinterest_client is None
        assert isinstance(app.state_repo, SqliteStateRepository)
        assert app.runner.pinterest_client is None
        app.close()

    def test_components_are_wired_from_settings(self, tmp_path):
        config = settings(
            tmp_path,
            min_image_width=1000,
            max_image_file_size_mb=8,
            movement_confidence_auto_threshold=0.9,
            allow_medium_confidence_routing=True,
        )
        app = build_application(config)

        assert isinstance(app.runner.media_validator, MediaValidator)
        assert app.runner.media_validator.requirements.min_width == 1000
        assert app.runner.media_validator.requirements.max_file_size_bytes == 8 * 1024 * 1024
        assert app.runner.rights_evaluator.enforce_strict is True
        assert app.runner.router._allow_medium is True
        assert app.runner.curator is not None
        assert set(app.runner.providers) == set(app.registry.source_ids)
        app.close()

    def test_in_memory_state_option(self, tmp_path):
        app = build_application(settings(tmp_path), in_memory_state=True)
        assert not (tmp_path / "state.db").exists()
        app.close()

    def test_shipped_boards_config_is_loaded(self, tmp_path):
        app = build_application(settings(tmp_path))
        assert len(app.boards_config.boards) > 10
        app.close()

    def test_missing_boards_file_falls_back_to_defaults(self, tmp_path):
        app = build_application(settings(tmp_path, boards_config_path=str(tmp_path / "nope.yaml")))
        assert len(app.boards_config.boards) > 10
        app.close()


class TestSchedulerGate:
    def test_refuses_to_start_when_disabled(self, tmp_path):

        app = build_application(settings(tmp_path))
        scheduler = Scheduler(app.runner, app.state_repo, app.settings)
        with pytest.raises(ScheduleDisabledError, match="PINTEREST_PRODUCTION_SCHEDULE_ENABLED"):
            asyncio.run(scheduler.run_forever())
        app.close()

    def test_runs_configured_number_of_iterations(self, tmp_path):

        app = build_application(
            settings(
                tmp_path,
                pinterest_production_schedule_enabled=True,
                scheduler_max_iterations=2,
                scheduler_interval_seconds=1,
                default_museum="artic",
            )
        )

        calls: list[DiscoveryRequest] = []

        async def fake_run(request: DiscoveryRequest):
            calls.append(request)
            from pinterest_art_bot.pipeline.models import PipelineRunSummary

            return PipelineRunSummary(run_id="run_x", museum=request.museum)

        app.runner.run = fake_run  # type: ignore[method-assign]
        scheduler = Scheduler(app.runner, app.state_repo, app.settings)
        results = asyncio.run(scheduler.run_forever())

        assert len(results) == 2
        assert len(calls) == 2
        assert calls[0].publish is False  # publishing stays off
        app.close()

    def test_run_once_isolates_failures(self, tmp_path):
        app = build_application(settings(tmp_path))
        runner = app.runner

        async def boom(request: DiscoveryRequest):
            raise RuntimeError("museum down")

        runner.run = boom  # type: ignore[method-assign]
        scheduler = Scheduler(runner, app.state_repo, app.settings)
        result = asyncio.run(scheduler.run_once())

        assert result.errors == 1
        assert result.published == 0
        app.close()


def test_scheduler_stop_event(tmp_path):

    app = build_application(settings(tmp_path))
    scheduler = Scheduler(app.runner, app.state_repo, app.settings)
    scheduler.request_stop()
    assert isinstance(scheduler, Scheduler)
    app.close()


class TestSchedulerLockIntegration:
    """The scheduler holds a single-instance lock for its lifetime."""

    async def test_second_scheduler_is_refused(self, tmp_path):
        from pinterest_art_bot.pipeline.lock import LockUnavailableError, ProcessLock
        from pinterest_art_bot.pipeline.scheduler import Scheduler

        app = build_application(
            settings(
                tmp_path,
                pinterest_production_schedule_enabled=True,
                scheduler_max_iterations=1,
                scheduler_lock_path=str(tmp_path / "scheduler.lock"),
            )
        )
        first = Scheduler(
            app.runner, app.state_repo, app.settings, lock=ProcessLock(tmp_path / "a.lock")
        )
        first._lock.acquire()

        second = Scheduler(
            app.runner, app.state_repo, app.settings, lock=ProcessLock(tmp_path / "a.lock")
        )
        with pytest.raises(LockUnavailableError):
            await second.run_forever()

        first.release()
        app.close()

    def test_lock_path_comes_from_settings(self, tmp_path):
        config = settings(tmp_path, scheduler_lock_path=str(tmp_path / "custom.lock"))
        assert config.scheduler_lock_path.endswith("custom.lock")

    def test_release_is_safe_without_a_run(self, tmp_path):
        from pinterest_art_bot.pipeline.scheduler import Scheduler

        app = build_application(settings(tmp_path))
        scheduler = Scheduler(app.runner, app.state_repo, app.settings)
        scheduler.release()  # must not raise
        app.close()


class TestNewSettings:
    def test_retention_defaults(self):
        config = Settings(_env_file=None)  # type: ignore[call-arg]
        assert config.state_run_retention_seconds == 2592000
        assert config.state_fingerprint_retention_seconds == 15552000
        assert config.reconcile_max_records == 20
        assert config.reconcile_max_pages_per_board == 10

    def test_retention_can_be_disabled(self):
        config = Settings(  # type: ignore[call-arg]
            _env_file=None, state_run_retention_seconds=0, state_fingerprint_retention_seconds=0
        )
        assert config.state_run_retention_seconds == 0
        assert config.state_fingerprint_retention_seconds == 0

    def test_reconcile_limits_are_validated(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, reconcile_max_records=0)  # type: ignore[call-arg]

    def test_status_exposes_retention_and_reconcile_budgets(self, capsys, isolated_env=None):
        import json as _json

        from pinterest_art_bot.cli import main

        assert main(["status", "--json"]) == 0
        payload = _json.loads(capsys.readouterr().out)
        assert "retention" in payload
        assert payload["reconcile"]["max_records"] == 20
        assert payload["retention"]["runs_seconds"] == 2592000
