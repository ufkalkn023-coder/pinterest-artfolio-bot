"""Shared test fixtures and helpers."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.museums.base import DiscoveryPage


def make_test_jpeg(width: int = 800, height: int = 800, color: str = "green") -> bytes:
    """Generate a valid in-memory JPEG of the requested size."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def test_image_bytes() -> bytes:
    """A valid 800x800 JPEG for media validation tests."""
    return make_test_jpeg()


class FakeMuseumProvider:
    """Configurable mock museum provider for pipeline tests."""

    def __init__(self, artworks: list[Artwork] | None = None) -> None:
        self._artworks = artworks or []
        self.discover_calls: list[dict] = []
        self.search_calls: list[dict] = []

    @property
    def source_id(self) -> str:
        return "fake_museum"

    @property
    def museum_name(self) -> str:
        return "Fake Museum of Art"

    async def discover(
        self,
        page_size: int = 20,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> DiscoveryPage:
        self.discover_calls.append({"page_size": page_size, "offset": offset})
        start = int(offset or 0)
        return DiscoveryPage(
            artworks=self._artworks[start : start + page_size],
            total_count=len(self._artworks),
            has_more=start + page_size < len(self._artworks),
            next_offset=start + page_size if start + page_size < len(self._artworks) else None,
        )

    async def get_artwork(self, object_id: str) -> Artwork | None:
        for art in self._artworks:
            if art.identity.source_object_id == object_id:
                return art
        return None

    async def search(self, query) -> DiscoveryPage:
        self.search_calls.append({"query": query.query, "page_size": query.page_size})
        return DiscoveryPage(
            artworks=self._artworks[: query.page_size],
            total_count=len(self._artworks),
        )


@pytest.fixture
def sample_artwork() -> Artwork:
    """A well-formed public-domain artwork for testing."""
    return Artwork(
        identity=ArtworkIdentity(source="met", source_object_id="436535"),
        title="Wheat Field with Cypresses",
        artist_name="Vincent van Gogh",
        artist_id="500010800",
        creation_date_text="1889",
        year_start=1889,
        year_end=1889,
        culture="Dutch",
        department="European Paintings",
        classification="Paintings",
        object_type="Painting",
        medium="Oil on canvas",
        period_raw=None,
        style_raw="Post-Impressionist",
        museum_name="The Metropolitan Museum of Art",
        museum_object_url="https://www.metmuseum.org/art/collection/search/436535",
        primary_image=ImageInfo(
            url="https://images.metmuseum.org/example/436535.jpg",
            width=2048,
            height=1536,
        ),
        thumbnail_url="https://images.metmuseum.org/example/436535_small.jpg",
        public_domain=True,
        open_access=True,
        credit_line="Purchase, The Annenberg Foundation Gift, 1993",
    )


@pytest.fixture
def copyrighted_artwork() -> Artwork:
    """An artwork that is NOT public domain."""
    return Artwork(
        identity=ArtworkIdentity(source="met", source_object_id="999999"),
        title="Copyrighted Painting",
        artist_name="Living Artist",
        creation_date_text="2020",
        year_start=2020,
        year_end=2020,
        museum_name="The Metropolitan Museum of Art",
        public_domain=False,
        open_access=False,
        rights_text="© Living Artist. All rights reserved.",
    )


@pytest.fixture
def unknown_rights_artwork() -> Artwork:
    """An artwork with no rights information."""
    return Artwork(
        identity=ArtworkIdentity(source="met", source_object_id="888888"),
        title="Mystery Painting",
        artist_name="Unknown Artist",
        museum_name="Test Museum",
        primary_image=ImageInfo(
            url="https://example.com/mystery.jpg",
            width=1000,
            height=800,
        ),
    )


@pytest.fixture
def baroque_artwork() -> Artwork:
    """A clearly Baroque artwork for classification testing."""
    return Artwork(
        identity=ArtworkIdentity(source="met", source_object_id="437397"),
        title="Self-Portrait with a Straw Hat",
        artist_name="Rembrandt",
        creation_date_text="1628",
        year_start=1628,
        year_end=1628,
        culture="Dutch",
        department="European Paintings",
        classification="Paintings",
        medium="Oil on panel",
        style_raw="Baroque",
        museum_name="The Metropolitan Museum of Art",
        public_domain=True,
        open_access=True,
        primary_image=ImageInfo(
            url="https://images.metmuseum.org/example/437397.jpg",
            width=1800,
            height=2200,
        ),
    )


@pytest.fixture
def impressionist_artwork() -> Artwork:
    """An Impressionist artwork for classification testing."""
    return Artwork(
        identity=ArtworkIdentity(source="artic", source_object_id="16568"),
        title="Water Lilies",
        artist_name="Claude Monet",
        creation_date_text="1906",
        year_start=1906,
        year_end=1906,
        department="Painting and Sculpture of Europe",
        medium="Oil on canvas",
        style_raw="Impressionism",
        museum_name="Art Institute of Chicago",
        public_domain=True,
        open_access=True,
        primary_image=ImageInfo(
            url="https://www.artic.edu/iiif/2/example/full/843,/0/default.jpg",
            width=2000,
            height=1500,
        ),
    )
