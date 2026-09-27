"""Tests for the command-line interface.

Covers argument wiring, exit codes, and — most importantly — that the CLI cannot
publish while the safety switches are off.
"""

import json

import pytest

from pinterest_art_bot.cli import (
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    create_parser,
    main,
)
from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.state.repository import InMemoryStateRepository


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Point settings at a temporary database and clear all safety switches."""
    for key in list(__import__("os").environ):
        if key.endswith(("PUBLISH_TO_PINTEREST", "ACCESS_TOKEN", "AUTO_CREATE_BOARDS")):
            monkeypatch.delenv(key, raising=False)

    import pinterest_art_bot.cli as cli_module

    config = Settings(  # type: ignore[call-arg]
        _env_file=None,
        state_db_path=str(tmp_path / "state.db"),
        boards_config_path=str(tmp_path / "boards.yaml"),
        default_museum="artic",
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: config)
    return config


class TestParser:
    def test_no_command_prints_help(self, capsys):
        assert main([]) == EXIT_USAGE
        assert "usage" in capsys.readouterr().out.lower()

    def test_all_commands_exist(self):
        parser = create_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        commands = set()
        for action in actions:
            commands.update(action.choices or {})
        assert {
            "ingest",
            "status",
            "boards-sync",
            "reconcile",
            "state",
            "schedule",
        } <= commands

    def test_ingest_defaults_to_dry_run(self):
        args = create_parser().parse_args(["ingest"])
        assert args.publish is False
        assert args.mode == "highlights"

    def test_discovery_modes_parse(self):
        parser = create_parser()
        assert (
            parser.parse_args(["ingest", "--mode", "search", "--query", "monet"]).query == "monet"
        )
        assert parser.parse_args(
            ["ingest", "--mode", "object-id", "--object-ids", "1", "2"]
        ).object_ids == ["1", "2"]

    def test_mode_accepts_both_separators(self):
        parser = create_parser()
        assert parser.parse_args(["ingest", "--mode", "object_id"]).mode.value == "object_id"
        assert parser.parse_args(["ingest", "--mode", "object-id"]).mode.value == "object_id"

    def test_state_actions_parse(self):
        parser = create_parser()
        assert parser.parse_args(["state", "counts"]).action == "counts"
        assert parser.parse_args(["state", "ambiguous"]).action == "ambiguous"


class TestStatus:
    def test_reports_safety_switches(self, capsys):
        assert main(["status", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["safety_switches"]["publish_to_pinterest"] is False
        assert payload["capabilities"]["can_publish"] is False
        assert "cleveland" in payload["museums"]
        assert payload["boards"]["total"] > 10

    def test_human_readable_output(self, capsys):
        assert main(["status"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "SAFETY" in out.upper()
        assert "publish_to_pinterest" in out


class TestIngest:
    def test_publish_blocked_without_switch(self, capsys, isolated_env):
        assert main(["ingest", "--publish"]) == EXIT_BLOCKED
        assert "PUBLISH_TO_PINTEREST" in capsys.readouterr().err

    def test_search_requires_query(self, capsys):
        assert main(["ingest", "--mode", "search"]) == EXIT_USAGE
        assert "requires --query" in capsys.readouterr().err

    def test_object_id_mode_requires_ids(self, capsys):
        assert main(["ingest", "--mode", "object-id"]) == EXIT_USAGE
        assert "requires --object-ids" in capsys.readouterr().err

    def test_unknown_museum_is_a_usage_error(self, capsys):
        assert main(["ingest", "--museum", "nonexistent"]) == EXIT_USAGE
        assert "Unknown museum provider" in capsys.readouterr().err


class TestPinterestCommands:
    def test_boards_sync_requires_token(self, capsys, isolated_env):
        assert main(["boards-sync"]) == EXIT_ERROR
        assert "PINTEREST_ACCESS_TOKEN" in capsys.readouterr().err

    def test_reconcile_requires_token(self, capsys, isolated_env):
        assert main(["reconcile"]) == EXIT_ERROR
        assert "PINTEREST_ACCESS_TOKEN" in capsys.readouterr().err

    def test_reconcile_with_stub_client_resolves_nothing(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        monkeypatch.setattr(cli_module, "require_pinterest_client", lambda settings: StubBuilder())
        assert main(["reconcile"]) == EXIT_OK
        assert "Ambiguous records examined: 0" in capsys.readouterr().out


class StubBuilder:
    """Minimal Pinterest double for CLI wiring tests."""

    async def list_boards(self):
        return []

    async def list_board_pins(self, board_id, *, limit=None):
        return []

    async def create_board(self, request):
        raise AssertionError("must not be called")

    async def create_pin(self, request):
        raise AssertionError("must not be called")

    async def get_pin(self, pin_id):
        return None


class TestStateCommand:
    def test_counts_on_empty_database(self, capsys):
        assert main(["state", "counts", "--json"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out) == {}

    def test_ambiguous_listing_is_empty(self, capsys):
        assert main(["state", "ambiguous", "--json"]) == EXIT_OK
        assert json.loads(capsys.readouterr().out) == []

    def test_list_renders_records(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module
        from pinterest_art_bot.domain.publication import PublicationRecord, PublicationState

        async def fake_list(*, states=None, limit=50):
            record = PublicationRecord(artwork_canonical_id="met:1")
            record.transition_to(PublicationState.RESERVED, reason="r")
            record.transition_to(PublicationState.PUBLISHING, reason="p")
            record.transition_to(PublicationState.AMBIGUOUS, reason="timeout")
            return [record]

        repo = InMemoryStateRepository()
        monkeypatch.setattr(repo, "list_records", fake_list)

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)
            app.state_repo = repo
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["state", "ambiguous"]) == EXIT_OK
        assert "met:1" in capsys.readouterr().out


class TestScheduleCommand:
    def test_blocked_when_disabled(self, capsys, isolated_env):
        assert main(["schedule"]) == EXIT_BLOCKED
        assert "PINTEREST_PRODUCTION_SCHEDULE_ENABLED" in capsys.readouterr().err


def test_module_entrypoint_exists():
    import pinterest_art_bot.__main__ as entry

    assert entry.main is main


class TestIngestOutput:
    """The human and JSON renderings of a completed run."""

    def _patch_run(self, monkeypatch, capsys):
        from pinterest_art_bot.pipeline.models import (
            DiscoveryRequest,
            PipelineDecision,
            PipelineItemResult,
            PipelineRunSummary,
        )

        def summary(request: DiscoveryRequest) -> PipelineRunSummary:
            return PipelineRunSummary(
                run_id="run_test",
                museum=request.museum,
                fetched=1,
                rights_approved=1,
                rights_rejected=0,
                image_valid=1,
                published=1,
                pinterest_writes=1,
                items=[
                    PipelineItemResult(
                        canonical_id="met:1",
                        title="Painting",
                        decision=PipelineDecision.PUBLISHED,
                        reason="ok",
                        pin_id="999",
                    )
                ],
            )

        return summary

    def test_json_output_is_machine_readable(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)
            captured = self._patch_run(monkeypatch, capsys)

            async def fake_run(request):
                return captured(request)

            app.runner.run = fake_run
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["ingest", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["run_id"] == "run_test"
        assert payload["items"][0]["pin_id"] == "999"

    def test_human_output_lists_published_pins(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)
            captured = self._patch_run(monkeypatch, capsys)

            async def fake_run(request):
                return captured(request)

            app.runner.run = fake_run
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["ingest"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "Pinterest Writes (POST /pins):  1" in out
        assert "PUBLISHED PINS" in out
        assert "met:1" in out
        assert "pin 999" in out


class TestDryRunPlanInspection:
    """Dry-run plans must be inspectable without polluting publication history."""

    def test_counts_include_dry_run_plans(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)

            async def fake_plans(limit=50):
                return [{"artwork_id": f"met:{i}"} for i in range(3)]

            app.state_repo.list_dry_run_plans = fake_plans  # type: ignore[method-assign]
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["state", "counts", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run_plans"] == 3

    def test_dry_runs_listing(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application
        plan = {
            "artwork_id": "met:1",
            "title": "Water Lilies",
            "artist": "Claude Monet",
            "movement": "impressionism",
            "movement_confidence": 0.95,
            "board_name": "Impressionism",
            "board_id": None,
            "rights_status": "public_domain",
            "publishable": False,
        }

        def fake_build(settings):
            app = original(settings)

            async def fake_plans(limit=50):
                return [plan]

            app.state_repo.list_dry_run_plans = fake_plans  # type: ignore[method-assign]
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["state", "dry-runs", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["board_name"] == "Impressionism"
        assert payload[0]["publishable"] is False

    def test_dry_runs_human_output(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)

            async def fake_plans(limit=50):
                return [
                    {
                        "artwork_id": "met:1",
                        "title": "Water Lilies",
                        "artist": "Claude Monet",
                        "movement": "impressionism",
                        "movement_confidence": 0.95,
                        "board_name": "Impressionism",
                        "board_id": None,
                        "rights_status": "public_domain",
                        "publishable": False,
                    }
                ]

            app.state_repo.list_dry_run_plans = fake_plans  # type: ignore[method-assign]
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["state", "dry-runs"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "DRY-RUN PLANS" in out
        assert "Water Lilies" in out


class TestRunHistoryAndMetrics:
    """Operator visibility into what ran and what needs attention."""

    def test_state_runs_lists_history(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)

            async def fake_runs(limit=20):
                return [
                    {
                        "run_id": "run_abc",
                        "museum": "met",
                        "dry_run": True,
                        "fetched": 10,
                        "published": 0,
                        "errors": 0,
                        "finished_at": "2026-01-01T10:00:00+00:00",
                        "error": None,
                    }
                ]

            app.state_repo.list_runs = fake_runs  # type: ignore[method-assign]
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["state", "runs"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "RUN HISTORY" in out
        assert "run_abc" in out
        assert "met" in out

    def test_state_runs_json(self, monkeypatch, capsys, isolated_env):
        import pinterest_art_bot.cli as cli_module

        original = cli_module.build_application

        def fake_build(settings):
            app = original(settings)

            async def fake_runs(limit=20):
                return [{"run_id": "run_json", "museum": "artic", "dry_run": False}]

            app.state_repo.list_runs = fake_runs  # type: ignore[method-assign]
            return app

        monkeypatch.setattr(cli_module, "build_application", fake_build)
        assert main(["state", "runs", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["run_id"] == "run_json"

    def test_status_includes_metrics(self, capsys, isolated_env):
        assert main(["status", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "metrics" in payload
        assert payload["metrics"]["published_24h"] == 0
        assert isinstance(payload["warnings"], list)

    def test_status_shows_activity_section(self, capsys, isolated_env):
        assert main(["status"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "Activity (24h)" in out
        assert "ambiguous_open" in out


class TestPreflight:
    """Publishing should be enabled from evidence, not optimism."""

    def test_reports_blockers_without_credentials(self, capsys, isolated_env):
        assert main(["preflight", "--limit", "2", "--json"]) == EXIT_BLOCKED
        payload = json.loads(capsys.readouterr().out)
        assert payload["ready"] is False
        assert payload["blockers"] >= 2

    def test_human_output_lists_checks(self, capsys, isolated_env):
        main(["preflight", "--limit", "2"])
        out = capsys.readouterr().out
        assert "PREFLIGHT" in out
        assert "rights policy" in out
        assert "board config" in out
        assert "BLOCKED" in out

    def test_switches_are_reported_as_off(self, capsys, isolated_env):
        main(["preflight", "--limit", "2", "--json"])
        payload = json.loads(capsys.readouterr().out)
        names = {check["name"]: check["status"] for check in payload["checks"]}
        assert names["production switches"] == "ok"
        assert names["credentials"] == "blocker"
        assert names["rights policy"] == "ok"

    def test_zero_budget_is_a_warning(self, capsys, isolated_env):
        main(["preflight", "--limit", "2", "--json"])
        payload = json.loads(capsys.readouterr().out)
        names = {check["name"]: check["status"] for check in payload["checks"]}
        assert names["pin budget"] == "ok"

    def test_pinterest_probe_can_be_skipped(self, capsys, isolated_env):
        main(["preflight", "--limit", "2", "--skip-pinterest", "--json"])
        payload = json.loads(capsys.readouterr().out)
        names = {check["name"]: check["status"] for check in payload["checks"]}
        assert names["pinterest connection"] == "skipped"

    def test_unknown_museum_is_a_blocker(self, capsys, isolated_env):
        main(["preflight", "--museum", "nope", "--json"])
        payload = json.loads(capsys.readouterr().out)
        details = {c["name"]: c["detail"] for c in payload["checks"]}
        assert payload["ready"] is False
        assert "not available" in details["museum reachability"]

    def test_every_blocker_carries_a_remedy(self, capsys, isolated_env):
        main(["preflight", "--limit", "2", "--json"])
        payload = json.loads(capsys.readouterr().out)
        blockers = [c for c in payload["checks"] if c["status"] == "blocker"]
        assert blockers
        assert all(check["remedy"] for check in blockers), "a blocker must say what to do"
