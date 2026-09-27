"""Museum provider registry.

Central place to register and look up museum adapters by source_id.
Avoids hardcoding provider references throughout the pipeline.
"""

from __future__ import annotations

from pinterest_art_bot.museums.base import MuseumProvider


class MuseumRegistry:
    """Thread-safe registry of museum providers."""

    def __init__(self) -> None:
        self._providers: dict[str, MuseumProvider] = {}

    def register(self, provider: MuseumProvider) -> None:
        """Register a museum provider by its source_id."""
        if provider.source_id in self._providers:
            raise ValueError(f"Museum provider already registered: {provider.source_id}")
        self._providers[provider.source_id] = provider

    def get(self, source_id: str) -> MuseumProvider | None:
        """Look up a provider by source_id."""
        return self._providers.get(source_id)

    def get_all(self) -> list[MuseumProvider]:
        """Return all registered providers."""
        return list(self._providers.values())

    @property
    def source_ids(self) -> list[str]:
        """All registered source identifiers."""
        return list(self._providers.keys())

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._providers

    def __len__(self) -> int:
        return len(self._providers)
