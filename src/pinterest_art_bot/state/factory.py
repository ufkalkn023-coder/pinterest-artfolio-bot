"""State repository factory.

Selects the state backend from settings so the pipeline never hardcodes a
storage engine. The authoritative local backend is SQLite; `:memory:` is only
used when explicitly requested (tests, dry-run experiments).
"""

from __future__ import annotations

import structlog

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.state.repository import StateRepository
from pinterest_art_bot.state.sqlite import SqliteStateRepository

logger = structlog.get_logger()

IN_MEMORY = ":memory:"


def create_state_repository(
    settings: Settings,
    *,
    in_memory: bool = False,
) -> StateRepository:
    """Build the state repository described by settings."""
    if in_memory:
        logger.debug("state_repository_selected", backend="sqlite", path=IN_MEMORY)
        return SqliteStateRepository(IN_MEMORY)

    db_path = settings.state_db_file
    logger.debug("state_repository_selected", backend="sqlite", path=str(db_path))
    return SqliteStateRepository(db_path)
