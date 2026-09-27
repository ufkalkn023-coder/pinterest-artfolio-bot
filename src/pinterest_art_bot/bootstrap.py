"""Application bootstrap — the single place where components are wired together.

Layering rules enforced here:
  - Museum adapters never see Pinterest.
  - Pinterest code never sees museum payloads.
  - Configuration is read once and injected explicitly.
  - Safety wrappers are always the outermost Pinterest layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from pinterest_art_bot.boards.config import BoardsConfiguration, load_boards_config
from pinterest_art_bot.boards.router import BoardRouter
from pinterest_art_bot.classification.classifier import MovementClassifier
from pinterest_art_bot.config.settings import Settings, get_settings
from pinterest_art_bot.curation.eligibility import EligibilityEvaluator
from pinterest_art_bot.media.preparation import ImagePreparer, MediaMode
from pinterest_art_bot.media.validation import MediaRequirements, MediaValidator
from pinterest_art_bot.museums.factory import build_registry
from pinterest_art_bot.museums.registry import MuseumRegistry
from pinterest_art_bot.pinterest.client import (
    HttpPinterestClient,
    PinterestClient,
    SafePinterestClient,
)
from pinterest_art_bot.pinterest.errors import PinterestAuthError
from pinterest_art_bot.pipeline.pin_composer import PinComposer
from pinterest_art_bot.pipeline.pin_templates import load_pin_templates
from pinterest_art_bot.pipeline.runner import PipelineRunner
from pinterest_art_bot.rights.evaluator import RightsEvaluator
from pinterest_art_bot.state.factory import create_state_repository
from pinterest_art_bot.state.repository import StateRepository

logger = structlog.get_logger()


@dataclass
class Application:
    """The wired application graph."""

    settings: Settings
    registry: MuseumRegistry
    boards_config: BoardsConfiguration
    state_repo: StateRepository
    runner: PipelineRunner
    pinterest_client: PinterestClient | None = None

    def close(self) -> None:
        """Release resources owned by the application."""
        closer = getattr(self.state_repo, "close", None)
        if callable(closer):
            closer()


def build_pinterest_client(settings: Settings) -> PinterestClient | None:
    """Build the real Pinterest client, guarded by the safety wrapper.

    Returns None when no credentials are configured: read-only and dry-run
    workflows do not need Pinterest at all, and we never fabricate a client
    that would make publishing look successful.
    """
    if not settings.is_pinterest_configured:
        logger.info("pinterest_client_skipped", reason="missing_access_token")
        return None

    inner = HttpPinterestClient(settings)
    logger.info("pinterest_client_built", can_publish=settings.can_publish)
    return SafePinterestClient(inner, settings)


def require_pinterest_client(settings: Settings) -> PinterestClient:
    """Build a Pinterest client or fail loudly."""
    client = build_pinterest_client(settings)
    if client is None:
        raise PinterestAuthError(
            "PINTEREST_ACCESS_TOKEN is not configured. Pinterest operations require a token."
        )
    return client


def build_application(
    settings: Settings | None = None,
    *,
    in_memory_state: bool = False,
) -> Application:
    """Construct the full application graph from settings."""
    config = settings or get_settings()

    boards_config = load_boards_config(
        config.boards_config_file if config.boards_config_file.exists() else None
    )
    registry = build_registry(config)
    state_repo = create_state_repository(config, in_memory=in_memory_state)
    pinterest_client = build_pinterest_client(config)

    media_validator = MediaValidator(MediaRequirements.from_settings(config))
    image_preparer = (
        ImagePreparer(
            max_width=config.image_max_width,
            jpeg_quality=config.image_jpeg_quality,
            mode=MediaMode(config.pin_media_mode),
        )
        if config.image_prepare_enabled
        else None
    )
    curator = EligibilityEvaluator(media_validator)
    router = BoardRouter(
        boards_config,
        allow_medium_confidence=config.allow_medium_confidence_routing,
    )
    classifier = MovementClassifier(
        auto_threshold=config.movement_confidence_auto_threshold,
        minimum_threshold=config.movement_confidence_minimum,
    )
    templates_path = config.pin_templates_path
    pin_composer = PinComposer(
        title_max_length=config.pin_title_max_length,
        description_max_length=config.pin_description_max_length,
        alt_text_max_length=config.pin_alt_text_max_length,
        text_config=load_pin_templates(
            Path(templates_path) if Path(templates_path).exists() else None
        ),
        extra_keywords=list(config.pin_extra_keywords),
    )
    rights_evaluator = RightsEvaluator(
        enforce_strict=config.rights_enforce_strict,
        auto_publishable=config.auto_publishable_rights_statuses,
    )

    runner = PipelineRunner(
        registry=registry,
        rights_evaluator=rights_evaluator,
        media_validator=media_validator,
        classifier=classifier,
        router=router,
        boards_config=boards_config,
        curator=curator,
        pin_composer=pin_composer,
        image_preparer=image_preparer,
        state_repo=state_repo,
        pinterest_client=pinterest_client,
        settings=config,
    )

    logger.info(
        "application_built",
        publish_to_pinterest=config.can_publish,
        auto_create_boards=config.can_create_boards,
        schedule_enabled=config.can_schedule,
        state_db=str(config.state_db_file),
        museums=registry.source_ids,
    )

    return Application(
        settings=config,
        registry=registry,
        boards_config=boards_config,
        state_repo=state_repo,
        runner=runner,
        pinterest_client=pinterest_client,
    )
