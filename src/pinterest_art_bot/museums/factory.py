"""Museum provider factory and default registry.

Single place where adapters are wired to configuration. Providers whose required
credentials are missing are skipped (and logged) instead of failing the run.
"""

from __future__ import annotations

import structlog

from pinterest_art_bot.config.settings import Settings, build_settings
from pinterest_art_bot.museums.artic.adapter import ArticMuseumProvider
from pinterest_art_bot.museums.base import MuseumProvider
from pinterest_art_bot.museums.cleveland.adapter import ClevelandMuseumProvider
from pinterest_art_bot.museums.met.adapter import MetMuseumProvider
from pinterest_art_bot.museums.registry import MuseumRegistry
from pinterest_art_bot.museums.rijksmuseum.adapter import RijksmuseumProvider

logger = structlog.get_logger()


def build_registry(settings: Settings | None = None) -> MuseumRegistry:
    """Build the registry of available museum providers.

    Adapters that require an API key are only registered when the key is present.
    """
    config = settings or build_settings(load_env_file=False)
    registry = MuseumRegistry()

    registry.register(ArticMuseumProvider())
    registry.register(MetMuseumProvider())
    registry.register(ClevelandMuseumProvider())

    if config.rijksmuseum_api_key.strip():
        registry.register(RijksmuseumProvider(config.rijksmuseum_api_key))
    else:
        logger.info("museum_provider_skipped", source_id="rijksmuseum", reason="missing_api_key")

    logger.info("museum_registry_built", source_ids=registry.source_ids)
    return registry


def default_providers(settings: Settings | None = None) -> dict[str, MuseumProvider]:
    """Return available providers as the mapping shape used by the pipeline."""
    registry = build_registry(settings)
    return {provider.source_id: provider for provider in registry.get_all()}
