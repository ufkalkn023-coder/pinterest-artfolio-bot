"""Pinterest board synchronisation.

Maps local board configuration (config/boards.yaml) to real Pinterest board IDs.

INVARIANT: business logic never hardcodes board IDs. IDs are discovered here by
matching configured board names against the authenticated account's boards, and
persisted back into the board configuration file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, Field

from pinterest_art_bot.boards.config import BoardConfig, BoardsConfiguration
from pinterest_art_bot.pinterest.client import PinterestClient
from pinterest_art_bot.pinterest.models import CreateBoardRequest, PinterestBoard

logger = structlog.get_logger()


class BoardMatch(BaseModel):
    """A configured board matched to a Pinterest board."""

    board_key: str
    board_name: str
    board_id: str
    was_created: bool = False


class BoardSyncReport(BaseModel):
    """Outcome of a board synchronisation pass."""

    matched: list[BoardMatch] = Field(default_factory=list)
    unmatched_keys: list[str] = Field(default_factory=list)
    disabled_keys: list[str] = Field(default_factory=list)
    remote_boards_without_config: list[str] = Field(default_factory=list)
    ambiguous_names: list[str] = Field(
        default_factory=list,
        description="Configured boards skipped because Pinterest has same-named duplicates.",
    )
    total_remote: int = 0

    @property
    def matched_count(self) -> int:
        return len(self.matched)

    @property
    def unresolved_count(self) -> int:
        return len(self.unmatched_keys)


def normalize_board_name(name: str) -> str:
    """Normalise a board name for tolerant matching."""
    return " ".join(name.strip().lower().replace("_", " ").split())


async def fetch_remote_boards(client: PinterestClient) -> list[PinterestBoard]:
    """List all boards on the authenticated account."""
    return await client.list_boards()


async def sync_board_ids(
    config: BoardsConfiguration,
    client: PinterestClient,
    *,
    create_missing: bool = False,
) -> BoardSyncReport:
    """Resolve board IDs for every enabled board in the configuration.

    Matching is case/whitespace insensitive on the board name. When
    ``create_missing`` is True, unmatched boards are created on Pinterest
    (requires the board-creation safety switch to be enabled in the client).
    """
    remote_boards = await fetch_remote_boards(client)
    report = BoardSyncReport(total_remote=len(remote_boards))

    # Two boards with the same normalised name cannot be told apart safely, so
    # neither is matched: guessing could route Pins to the wrong board.
    by_name: dict[str, PinterestBoard] = {}
    ambiguous_names: set[str] = set()
    for board in remote_boards:
        key = normalize_board_name(board.name)
        if key in by_name:
            ambiguous_names.add(key)
            logger.warning(
                "board_name_ambiguous",
                board_name=board.name,
                board_ids=[by_name[key].id, board.id],
            )
            continue
        by_name[key] = board
    for key in ambiguous_names:
        by_name.pop(key, None)

    matched_remote_ids: set[str] = set()

    for board_config in config.boards:
        if not board_config.enabled:
            report.disabled_keys.append(board_config.board_key)
            continue

        remote = by_name.get(normalize_board_name(board_config.board_name))
        if remote is None and normalize_board_name(board_config.board_name) in ambiguous_names:
            report.ambiguous_names.append(board_config.board_name)
            continue
        if remote is None and create_missing:
            created = await client.create_board(
                CreateBoardRequest(
                    name=board_config.board_name,
                    description=board_config.description,
                )
            )
            remote = created
            logger.info(
                "board_created_on_pinterest",
                board_key=board_config.board_key,
                board_id=created.id,
            )

        if remote is None:
            report.unmatched_keys.append(board_config.board_key)
            logger.warning(
                "board_unmatched",
                board_key=board_config.board_key,
                board_name=board_config.board_name,
            )
            continue

        board_config.board_id = remote.id
        matched_remote_ids.add(remote.id)
        report.matched.append(
            BoardMatch(
                board_key=board_config.board_key,
                board_name=board_config.board_name,
                board_id=remote.id,
                was_created=remote not in remote_boards,
            )
        )

    configured_names = {normalize_board_name(b.board_name) for b in config.boards if b.enabled}
    report.remote_boards_without_config = [
        board.name
        for board in remote_boards
        if normalize_board_name(board.name) not in configured_names
    ]

    logger.info(
        "board_sync_completed",
        matched=report.matched_count,
        unmatched=report.unresolved_count,
        total_remote=report.total_remote,
    )
    return report


def to_yaml_document(config: BoardsConfiguration) -> dict[str, Any]:
    """Serialise board configuration into the on-disk YAML document shape."""
    document: dict[str, Any] = {"boards": {}}
    for board in config.boards:
        entry: dict[str, Any] = {
            "board_name": board.board_name,
            "board_id": board.board_id,
            "movements": [movement.value for movement in board.movements],
        }
        if board.description:
            entry["description"] = board.description
        if not board.enabled:
            entry["enabled"] = False
        document["boards"][board.board_key] = entry
    return document


def write_boards_config(config: BoardsConfiguration, path: Path) -> None:
    """Persist board configuration, preserving the file as much as possible.

    A resolved board ID is the only thing that changes during a sync, so the
    existing file is edited in place: hand-written comments, key order, and
    formatting all survive. A full rewrite happens only when the file cannot be
    patched, and says so.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""

    if existing:
        patched = _patch_board_ids(existing, config)
        if patched is not None:
            path.write_text(patched)
            return
        logger.warning(
            "boards_config_rewrite_required",
            path=str(path),
            reason="board_id lines could not be located; rewriting the file",
        )

    header = _HEADER
    if existing.startswith("#"):
        header = existing.split("boards:")[0]
    payload = yaml.safe_dump(to_yaml_document(config), sort_keys=False, allow_unicode=True)
    path.write_text(f"{header}{payload}")


def _patch_board_ids(text: str, config: BoardsConfiguration) -> str | None:
    """Replace each board's board_id value in place.

    Returns None when the layout is not recognised, so the caller can fall back to
    a full rewrite instead of silently corrupting the file.
    """
    lines = text.splitlines(keepends=True)
    board_ids = {
        board.board_key: board.board_id for board in config.boards if board.board_id is not None
    }
    if not board_ids:
        return text

    current_key: str | None = None
    patched_any = False
    out: list[str] = []

    for line in lines:
        key_match = re.match(r"^  ([A-Za-z0-9_\-]+):\s*$", line)
        if key_match:
            current_key = key_match.group(1)

        if current_key in board_ids and re.match(r"^    board_id:", line):
            new_value = board_ids[current_key]
            indent = line[: len(line) - len(line.lstrip())]
            comment = ""
            if "#" in line:
                comment = "  #" + line.split("#", 1)[1].rstrip()
            out.append(
                f"{indent}board_id: {json.dumps(new_value) if new_value else 'null'}{comment}\n"
            )
            patched_any = True
            current_key = None
            continue

        out.append(line)

    if not patched_any:
        return None
    return "".join(out)


def resolve_board_id(config: BoardsConfiguration, board_key: str) -> str | None:
    """Look up a resolved board ID by configuration key."""
    board: BoardConfig | None = config.get_board_by_key(board_key)
    return board.board_id if board else None


_HEADER = """# Pinterest Art Bot — Board Configuration
#
# Maps art movements to Pinterest boards.
# board_id values are filled in by `pinterest-art-bot boards-sync`.
# Do NOT hardcode board_id values in Python code.

"""
