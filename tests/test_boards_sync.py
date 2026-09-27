"""Tests for board synchronisation and the board config file round-trip."""

import httpx
import pytest

from pinterest_art_bot.boards.config import BoardsConfiguration, load_boards_config
from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.domain.movement import ArtMovement
from pinterest_art_bot.pinterest.boards_sync import (
    normalize_board_name,
    resolve_board_id,
    sync_board_ids,
    to_yaml_document,
    write_boards_config,
)
from pinterest_art_bot.pinterest.client import HttpPinterestClient, SafePinterestClient
from pinterest_art_bot.pinterest.errors import ProductionWriteDisabledError
from pinterest_art_bot.pinterest.models import CreateBoardRequest


def settings(**overrides) -> Settings:
    """Settings with Pinterest credentials and publishing enabled."""
    base = {
        "_env_file": None,
        "publish_to_pinterest": True,
        "pinterest_access_token": "token",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def http_client(payload: dict) -> HttpPinterestClient:
    """HTTP client whose transport returns a fixed payload."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    client = httpx.AsyncClient(transport=transport, base_url="https://api.pinterest.com/v5")
    return HttpPinterestClient(settings(), client=client)


class TestNameMatching:
    def test_normalisation_ignores_case_and_spacing(self):
        assert normalize_board_name("  Post-Impressionism ") == "post-impressionism"
        assert normalize_board_name("post_impressionism") == "post impressionism"

    async def test_matching_is_tolerant(self):
        config = BoardsConfiguration.model_validate(
            {
                "boards": [
                    {
                        "board_key": "baroque",
                        "board_name": "Baroque Art",
                        "movements": ["baroque"],
                    }
                ]
            }
        )
        client = http_client({"items": [{"id": "77", "name": "  baroque   ART "}]})
        report = await sync_board_ids(config, client)

        assert report.matched_count == 1
        assert config.boards[0].board_id == "77"
        assert resolve_board_id(config, "baroque") == "77"

    async def test_unmatched_board_is_reported(self):
        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "baroque", "board_name": "Baroque Art"}]}
        )
        client = http_client({"items": [{"id": "1", "name": "Renaissance Art"}]})
        report = await sync_board_ids(config, client)

        assert report.unmatched_keys == ["baroque"]
        assert report.remote_boards_without_config == ["Renaissance Art"]
        assert config.boards[0].board_id is None

    async def test_disabled_boards_are_skipped(self):
        config = BoardsConfiguration.model_validate(
            {
                "boards": [
                    {
                        "board_key": "baroque",
                        "board_name": "Baroque Art",
                        "enabled": False,
                    }
                ]
            }
        )
        client = http_client({"items": []})
        report = await sync_board_ids(config, client)

        assert report.disabled_keys == ["baroque"]
        assert report.matched == []


class TestBoardCreation:
    async def test_creation_is_opt_in(self):
        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "baroque", "board_name": "Baroque Art"}]}
        )
        client = http_client({"items": []})
        report = await sync_board_ids(config, client, create_missing=False)
        assert report.unmatched_keys == ["baroque"]

    async def test_creation_requires_safety_switch(self):
        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "baroque", "board_name": "Baroque Art"}]}
        )
        from pinterest_art_bot.pinterest.client import StubPinterestClient

        safe = SafePinterestClient(
            StubPinterestClient(), settings(pinterest_auto_create_boards=False)
        )
        with pytest.raises(ProductionWriteDisabledError):
            await sync_board_ids(config, safe, create_missing=True)

    async def test_creation_when_enabled(self):
        config = BoardsConfiguration.model_validate(
            {
                "boards": [
                    {
                        "board_key": "baroque",
                        "board_name": "Baroque Art",
                        "description": "Baroque art",
                    }
                ]
            }
        )
        from pinterest_art_bot.pinterest.client import StubPinterestClient

        safe = SafePinterestClient(
            StubPinterestClient(), settings(pinterest_auto_create_boards=True)
        )
        report = await sync_board_ids(config, safe, create_missing=True)

        assert report.matched_count == 1
        assert config.boards[0].board_id == "1000"
        assert report.matched[0].was_created is True


class TestYamlRoundTrip:
    def test_document_shape_matches_loader(self, tmp_path):
        original = load_boards_config(None)
        path = tmp_path / "boards.yaml"
        write_boards_config(original, path)

        reloaded = load_boards_config(path)
        assert len(reloaded.boards) == len(original.boards)
        for before, after in zip(original.boards, reloaded.boards, strict=True):
            assert before.board_key == after.board_key
            assert before.board_name == after.board_name
            assert before.movements == after.movements
            assert before.enabled == after.enabled

    def test_header_comment_is_preserved(self, tmp_path):
        path = tmp_path / "boards.yaml"
        path.write_text("# my custom header\n\nboards:\n  x:\n    board_name: X\n")
        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "x", "board_name": "X"}]}
        )
        write_boards_config(config, path)
        content = path.read_text()
        assert content.startswith("# my custom header")
        assert "board_name: X" in content

    def test_document_contains_movements_and_ids(self):
        config = BoardsConfiguration.model_validate(
            {
                "boards": [
                    {
                        "board_key": "impressionism",
                        "board_name": "Impressionism",
                        "board_id": "55",
                        "movements": ["impressionism", "neo_impressionism"],
                    }
                ]
            }
        )
        document = to_yaml_document(config)
        entry = document["boards"]["impressionism"]
        assert entry["board_id"] == "55"
        assert entry["movements"] == ["impressionism", "neo_impressionism"]
        assert ArtMovement.IMPRESSIONISM.value in entry["movements"]

    def test_shipped_config_file_loads(self):
        from pathlib import Path

        repo_config = Path(__file__).resolve().parents[1] / "config" / "boards.yaml"
        config = load_boards_config(repo_config)
        assert len(config.boards) > 10
        assert config.get_board_for_movement(ArtMovement.IMPRESSIONISM) is not None


class TestStubBoards:
    async def test_stub_list_and_create(self):
        from pinterest_art_bot.pinterest.client import StubPinterestClient

        client = StubPinterestClient()
        board = await client.create_board(CreateBoardRequest(name="Baroque Art"))
        boards = await client.list_boards()
        assert [b.id for b in boards] == [board.id]


class TestInPlaceUpdate:
    """A board sync must not destroy hand-written configuration."""

    def test_comments_and_order_survive(self, tmp_path):
        path = tmp_path / "boards.yaml"
        path.write_text(
            "# My header\n"
            "\n"
            "boards:\n"
            "\n"
            "  # the flagship board\n"
            "  baroque:\n"
            '    board_name: "Baroque Art"   # keep this inline note\n'
            "    board_id: null\n"
            '    description: "Baroque art"\n'
            "    movements:\n"
            "      - baroque\n"
            "\n"
            "  # another note\n"
            "  impressionism:\n"
            '    board_name: "Impressionism"\n'
            "    board_id: null\n"
            "    movements:\n"
            "      - impressionism\n"
        )

        config = load_boards_config(path)
        config.get_board_by_key("baroque").board_id = "111"
        config.get_board_by_key("impressionism").board_id = "222"
        write_boards_config(config, path)

        text = path.read_text()
        assert "# My header" in text
        assert "# the flagship board" in text
        assert "# another note" in text
        assert "keep this inline note" in text
        assert 'board_id: "111"' in text
        assert 'board_id: "222"' in text
        # Key order is unchanged.
        assert text.index("baroque:") < text.index("impressionism:")

    def test_unrecognised_layout_falls_back_to_a_rewrite(self, tmp_path):
        path = tmp_path / "boards.yaml"
        path.write_text("boards:\n  baroque:\n    name: legacy-layout\n")

        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "baroque", "board_name": "Baroque Art", "board_id": "9"}]}
        )
        write_boards_config(config, path)

        # The file is rewritten into the canonical shape rather than corrupted.
        reloaded = load_boards_config(path)
        assert reloaded.get_board_by_key("baroque").board_id == "9"

    def test_null_ids_are_left_alone(self, tmp_path):
        path = tmp_path / "boards.yaml"
        path.write_text('boards:\n  baroque:\n    board_name: "Baroque Art"\n    board_id: null\n')
        config = load_boards_config(path)
        write_boards_config(config, path)
        assert "board_id: null" in path.read_text()

    def test_round_trip_after_patching(self, tmp_path):
        path = tmp_path / "boards.yaml"
        path.write_text(
            "boards:\n"
            "  impressionism:\n"
            '    board_name: "Impressionism"\n'
            "    board_id: null\n"
            "    movements:\n"
            "      - impressionism\n"
        )
        config = load_boards_config(path)
        config.get_board_by_key("impressionism").board_id = "555"
        write_boards_config(config, path)

        reloaded = load_boards_config(path)
        assert reloaded.get_board_by_key("impressionism").board_id == "555"
        assert reloaded.get_board_by_key("impressionism").movements == [ArtMovement.IMPRESSIONISM]


class TestAmbiguousRemoteNames:
    async def test_duplicate_names_are_not_matched(self):
        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "baroque", "board_name": "Baroque Art"}]}
        )
        client = http_client(
            {
                "items": [
                    {"id": "1", "name": "Baroque Art"},
                    {"id": "2", "name": "  baroque   ART "},
                ]
            }
        )
        report = await sync_board_ids(config, client)

        assert report.matched == [], "an ambiguous name must not be guessed"
        assert report.ambiguous_names == ["Baroque Art"]
        assert config.boards[0].board_id is None

    async def test_unique_names_still_match(self):
        config = BoardsConfiguration.model_validate(
            {"boards": [{"board_key": "baroque", "board_name": "Baroque Art"}]}
        )
        client = http_client({"items": [{"id": "77", "name": "Baroque Art"}]})
        report = await sync_board_ids(config, client)
        assert report.matched_count == 1
        assert report.ambiguous_names == []
