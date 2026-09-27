"""Board routing configuration.

Maps canonical art movements to Pinterest board targets.
Board IDs are populated from Pinterest after boards exist there.
Board names and mappings are configuration, not code.

This YAML-backed config can be loaded from a file or set in code.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from pinterest_art_bot.domain.movement import ArtMovement
from pinterest_art_bot.domain.publication import BoardTarget


class BoardConfig(BaseModel):
    """Configuration for a single Pinterest board."""

    board_key: str
    board_name: str
    board_id: str | None = None
    description: str | None = None
    movements: list[ArtMovement] = Field(default_factory=list)
    enabled: bool = True


class BoardsConfiguration(BaseModel):
    """Complete board routing configuration."""

    boards: list[BoardConfig] = Field(default_factory=list)

    def get_board_for_movement(self, movement: ArtMovement) -> BoardConfig | None:
        """Find the board configured for a given movement."""
        for board in self.boards:
            if not board.enabled:
                continue
            if movement in board.movements:
                return board
        return None

    def get_board_by_key(self, key: str) -> BoardConfig | None:
        """Look up a board by its configuration key."""
        for board in self.boards:
            if board.board_key == key:
                return board
        return None

    def to_board_target(self, config: BoardConfig) -> BoardTarget:
        """Convert a BoardConfig to a BoardTarget for publication."""
        return BoardTarget(
            board_key=config.board_key,
            board_name=config.board_name,
            board_id=config.board_id,
        )


def load_boards_config(path: Path | None = None) -> BoardsConfiguration:
    """Load board configuration from a YAML file.

    If no path is provided, returns the default configuration.
    """
    if path and path.exists():
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return _parse_config(data)
    return default_boards_config()


def _parse_config(data: dict[str, Any]) -> BoardsConfiguration:
    """Parse a YAML config dict into BoardsConfiguration."""
    boards: list[BoardConfig] = []
    for key, value in data.get("boards", {}).items():
        movements_raw = value.get("movements", [key])
        movements = []
        for m in movements_raw:
            with contextlib.suppress(ValueError):
                movements.append(ArtMovement(m.lower()))
        boards.append(
            BoardConfig(
                board_key=key,
                board_name=value.get("board_name", key.replace("_", " ").title()),
                board_id=value.get("board_id"),
                description=value.get("description"),
                movements=movements,
                enabled=value.get("enabled", True),
            )
        )
    return BoardsConfiguration(boards=boards)


def default_boards_config() -> BoardsConfiguration:
    """Default board configuration mapping movements to boards."""
    return BoardsConfiguration(
        boards=[
            BoardConfig(
                board_key="ancient",
                board_name="Ancient Art",
                movements=[ArtMovement.ANCIENT],
            ),
            BoardConfig(
                board_key="medieval",
                board_name="Medieval Art",
                movements=[ArtMovement.MEDIEVAL, ArtMovement.GOTHIC, ArtMovement.BYZANTINE],
            ),
            BoardConfig(
                board_key="renaissance",
                board_name="Renaissance Art",
                movements=[
                    ArtMovement.EARLY_RENAISSANCE,
                    ArtMovement.RENAISSANCE,
                    ArtMovement.HIGH_RENAISSANCE,
                    ArtMovement.NORTHERN_RENAISSANCE,
                    ArtMovement.MANNERISM,
                ],
            ),
            BoardConfig(
                board_key="baroque",
                board_name="Baroque Art",
                movements=[ArtMovement.BAROQUE],
            ),
            BoardConfig(
                board_key="rococo",
                board_name="Rococo Art",
                movements=[ArtMovement.ROCOCO],
            ),
            BoardConfig(
                board_key="neoclassicism",
                board_name="Neoclassicism",
                movements=[ArtMovement.NEOCLASSICISM],
            ),
            BoardConfig(
                board_key="romanticism",
                board_name="Romanticism",
                movements=[ArtMovement.ROMANTICISM],
            ),
            BoardConfig(
                board_key="realism",
                board_name="Realism",
                movements=[ArtMovement.REALISM],
            ),
            BoardConfig(
                board_key="impressionism",
                board_name="Impressionism",
                movements=[ArtMovement.IMPRESSIONISM, ArtMovement.NEO_IMPRESSIONISM],
            ),
            BoardConfig(
                board_key="post_impressionism",
                board_name="Post-Impressionism",
                movements=[ArtMovement.POST_IMPRESSIONISM],
            ),
            BoardConfig(
                board_key="art_nouveau",
                board_name="Art Nouveau",
                movements=[ArtMovement.ART_NOUVEAU, ArtMovement.ARTS_AND_CRAFTS],
            ),
            BoardConfig(
                board_key="symbolism",
                board_name="Symbolism",
                movements=[ArtMovement.SYMBOLISM],
            ),
            BoardConfig(
                board_key="expressionism",
                board_name="Expressionism",
                movements=[ArtMovement.EXPRESSIONISM, ArtMovement.FAUVISM],
            ),
            BoardConfig(
                board_key="cubism",
                board_name="Cubism",
                movements=[ArtMovement.CUBISM, ArtMovement.FUTURISM, ArtMovement.CONSTRUCTIVISM],
            ),
            BoardConfig(
                board_key="dada_surrealism",
                board_name="Surrealism",
                movements=[ArtMovement.DADA, ArtMovement.SURREALISM],
            ),
            BoardConfig(
                board_key="abstract_expressionism",
                board_name="Abstract Expressionism",
                movements=[ArtMovement.ABSTRACT_EXPRESSIONISM],
            ),
            BoardConfig(
                board_key="pop_art",
                board_name="Pop Art",
                movements=[ArtMovement.POP_ART, ArtMovement.OP_ART],
            ),
            BoardConfig(
                board_key="minimalism",
                board_name="Minimalism",
                movements=[ArtMovement.MINIMALISM],
            ),
            BoardConfig(
                board_key="modern",
                board_name="Modern Art",
                movements=[ArtMovement.MODERNISM, ArtMovement.ART_DECO],
            ),
            BoardConfig(
                board_key="contemporary",
                board_name="Contemporary Art",
                movements=[ArtMovement.CONTEMPORARY],
            ),
        ]
    )
