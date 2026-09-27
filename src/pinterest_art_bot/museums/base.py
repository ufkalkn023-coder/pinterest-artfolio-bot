"""Museum provider protocol — the contract every museum adapter must satisfy.

Each museum adapter translates its own API representation into our
canonical Artwork model.  Museum adapters must NOT know anything about
Pinterest, board routing, or publication.

Adapters are deliberately async to support concurrent museum queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pinterest_art_bot.domain.artwork import Artwork


@dataclass(frozen=True)
class DiscoveryPage:
    """A page of discovered artworks from a museum API.

    Supports cursor-based and offset-based pagination.
    """

    artworks: list[Artwork]
    total_count: int | None = None
    has_more: bool = False
    next_cursor: str | None = None
    next_offset: int | None = None


@dataclass(frozen=True)
class SearchQuery:
    """Parameters for searching a museum's collection."""

    query: str | None = None
    department: str | None = None
    classification: str | None = None
    medium: str | None = None
    culture: str | None = None
    date_start: int | None = None
    date_end: int | None = None
    has_images: bool = True
    public_domain_only: bool = True
    page_size: int = 20
    cursor: str | None = None
    offset: int | None = None


@runtime_checkable
class MuseumProvider(Protocol):
    """Protocol that every museum adapter must implement.

    Each method translates museum-specific API calls and responses
    into our canonical domain models.
    """

    @property
    def source_id(self) -> str:
        """Canonical source identifier, e.g. 'met', 'artic', 'rijksmuseum'."""
        ...

    @property
    def museum_name(self) -> str:
        """Human-readable museum name."""
        ...

    async def discover(
        self,
        *,
        page_size: int = 20,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> DiscoveryPage:
        """Discover artworks — typically public-domain, with images.

        This is the primary ingestion method for scheduled runs.
        Returns a page of canonical Artwork objects.
        """
        ...

    async def get_artwork(self, object_id: str) -> Artwork | None:
        """Fetch a single artwork by its source object ID.

        Returns None if the object does not exist.
        """
        ...

    async def search(self, query: SearchQuery) -> DiscoveryPage:
        """Search the museum collection with filters.

        Returns matching artworks as canonical Artwork objects.
        """
        ...
