"""Tests for museum adapters (ArtIC, Met) and shared HTTP client."""

import httpx
import pytest

from pinterest_art_bot.museums.artic.adapter import ArticMuseumProvider
from pinterest_art_bot.museums.http import (
    MuseumHttpClient,
    MuseumRateLimitError,
    MuseumTimeoutError,
)
from pinterest_art_bot.museums.met.adapter import MetMuseumProvider


class TestArticAdapter:
    """Art Institute of Chicago adapter normalization tests."""

    @pytest.fixture
    def artic_payload(self) -> dict:
        return {
            "id": 27992,
            "title": "A Sunday on La Grande Jatte — 1884",
            "artist_display": "Georges Seurat (French, 1859-1891)",
            "artist_id": 40610,
            "date_display": "1884/86",
            "date_start": 1884,
            "date_end": 1886,
            "department_title": "Painting and Sculpture of Europe",
            "classification_titles": ["painting"],
            "artwork_type_title": "Painting",
            "medium_display": "Oil on canvas",
            "style_title": "Post-Impressionism",
            "style_titles": ["Post-Impressionism", "Pointillism"],
            "category_titles": ["European Painting"],
            "image_id": "e966799b-97ee-eb44-1596-f6652410a880",
            "is_public_domain": True,
            "credit_line": "Helen Birch Bartlett Memorial Collection",
        }

    @pytest.mark.asyncio
    async def test_artic_translation(self, artic_payload):
        provider = ArticMuseumProvider()
        config = {"iiif_url": "https://www.artic.edu/iiif/2"}
        artwork = provider._translate(artic_payload, config)

        assert artwork.canonical_id == "artic:27992"
        assert artwork.title == "A Sunday on La Grande Jatte — 1884"
        assert artwork.artist_name == "Georges Seurat (French, 1859-1891)"
        assert artwork.year_start == 1884
        assert artwork.year_end == 1886
        assert artwork.style_raw == "Post-Impressionism"
        assert "Pointillism" in artwork.tags
        assert artwork.public_domain is True
        assert artwork.open_access is True
        assert artwork.primary_image is not None
        assert "e966799b-97ee-eb44-1596-f6652410a880" in artwork.primary_image.url
        assert artwork.credit_line == "Helen Birch Bartlett Memorial Collection"

    @pytest.mark.asyncio
    async def test_artic_discovery_filtering(self, artic_payload):
        """Discovery must filter for items that have image_id and are public domain."""
        item_without_image = dict(artic_payload)
        item_without_image["id"] = 99999
        item_without_image["image_id"] = None

        search_response = {
            "data": [artic_payload, item_without_image],
            "config": {"iiif_url": "https://www.artic.edu/iiif/2"},
            "pagination": {
                "total": 2,
                "limit": 2,
                "offset": 0,
                "current_page": 1,
                "total_pages": 1,
            },
        }

        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=search_response))
        async with httpx.AsyncClient(transport=transport) as client:
            provider = ArticMuseumProvider(http_client=client)
            page = await provider.discover(page_size=2)

        # The item without image_id must be excluded
        assert len(page.artworks) == 1
        assert page.artworks[0].canonical_id == "artic:27992"


class TestMetAdapter:
    """The Metropolitan Museum of Art adapter normalization tests."""

    @pytest.fixture
    def met_payload(self) -> dict:
        return {
            "objectID": 436535,
            "isHighlight": True,
            "isPublicDomain": True,
            "primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg",
            "primaryImageSmall": "https://images.metmuseum.org/CRDImages/ep/web-large/DT1567.jpg",
            "additionalImages": ["https://images.metmuseum.org/CRDImages/ep/original/DT1568.jpg"],
            "department": "European Paintings",
            "objectName": "Painting",
            "title": "Wheat Field with Cypresses",
            "culture": "",
            "period": "",
            "artistDisplayName": "Vincent van Gogh",
            "artistULAN_URL": "http://vocab.getty.edu/page/ulan/500115588",
            "objectDate": "1889",
            "objectBeginDate": 1889,
            "objectEndDate": 1889,
            "medium": "Oil on canvas",
            "classification": "Paintings",
            "objectURL": "https://www.metmuseum.org/art/collection/search/436535",
            "tags": [{"term": "Landscapes"}, {"term": "Cypresses"}],
            "creditLine": "Purchase, The Annenberg Foundation Gift, 1993",
        }

    def test_met_translation(self, met_payload):
        provider = MetMuseumProvider()
        artwork = provider._translate(met_payload)

        assert artwork.canonical_id == "met:436535"
        assert artwork.title == "Wheat Field with Cypresses"
        assert artwork.artist_name == "Vincent van Gogh"
        assert artwork.artist_id == "500115588"
        assert artwork.year_start == 1889
        assert artwork.year_end == 1889
        assert artwork.public_domain is True
        assert artwork.open_access is True
        assert artwork.primary_image is not None
        assert "DT1567.jpg" in artwork.primary_image.url
        assert len(artwork.additional_images) == 1
        assert "Landscapes" in artwork.tags
        assert "Cypresses" in artwork.tags
        assert artwork.credit_line == "Purchase, The Annenberg Foundation Gift, 1993"


class TestMuseumHttpClient:
    """Robust HTTP client with backoff, retries, and headers."""

    @pytest.mark.asyncio
    async def test_retry_on_503_then_success(self):
        attempts = 0

        def handler(request: httpx.Request):
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                return httpx.Response(503, content=b"Unavailable")
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            http = MuseumHttpClient(client=client, max_retries=3)
            data = await http.get_json("https://example.com/api")
            assert data == {"status": "ok"}
            assert attempts == 2

    @pytest.mark.asyncio
    async def test_exhaust_retries_on_rate_limit(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(429, headers={"Retry-After": "0.1"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            http = MuseumHttpClient(client=client, max_retries=2)
            with pytest.raises(MuseumRateLimitError, match="HTTP 429"):
                await http.get_json("https://example.com/api")

    @pytest.mark.asyncio
    async def test_timeout_retries_exhausted(self):
        def handler(request):
            raise httpx.ReadTimeout("Read timed out")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            http = MuseumHttpClient(client=client, max_retries=1)
            with pytest.raises(MuseumTimeoutError, match="Timeout connecting"):
                await http.get_json("https://example.com/api")
