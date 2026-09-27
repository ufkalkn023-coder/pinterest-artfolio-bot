"""Tests for the Cleveland and Rijksmuseum adapters, plus registry wiring.

The adapters must translate museum payloads into the canonical Artwork model and
must not leak Pinterest concerns. Rights fields are only set from explicit API
signals so the rights gate keeps its default-deny guarantee.
"""

from typing import ClassVar

import httpx
import pytest

from pinterest_art_bot.config.settings import Settings
from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.museums.base import MuseumProvider
from pinterest_art_bot.museums.cleveland.adapter import (
    CLEVELAND_BASE_URL,
    ClevelandMuseumProvider,
)
from pinterest_art_bot.museums.factory import build_registry, default_providers
from pinterest_art_bot.museums.rijksmuseum.adapter import RijksmuseumProvider

CLEVELAND_PAYLOAD = {
    "data": [
        {
            "id": 1234,
            "accession_number": "1942.662",
            "title": "The Cranes",
            "creators": [{"description": "Katsushika Hokusai", "role": "artist"}],
            "creation_date": "ca. 1830",
            "culture": "Japanese",
            "department": {"title": "Japanese Art"},
            "type": {"title": "Print"},
            "technique": "Color woodcut",
            "is_public_domain": True,
            "share_license_status": "CC0",
            "credit_line": "Purchase from the J. H. Wade Fund",
            "url": "https://www.clevelandart.org/art/1234",
            "images": {
                "web_image": "https://openaccess-cdn.clevelandart.org/1234.web.jpg",
                "thumbnail": "https://openaccess-cdn.clevelandart.org/1234.thumb.jpg",
            },
            "tags": [{"type": "style", "title": "Ukiyo-e"}],
            "description": "A pair of cranes in flight.",
        }
    ],
    "info": {"total": 1},
}

RIJKS_PAYLOAD = {
    "count": 137,
    "items": [
        {
            "objectNumber": "SK-A-4691",
            "title": "The Milkmaid",
            "principalMakers": [{"name": "Johannes Vermeer", "role": "artist"}],
            "dating": {"value": "1660", "period": "1660", "year": 1660},
            "yearFrom": 1660,
            "yearTo": 1660,
            "classification": {"value": "painting", "en": "painting"},
            "medium": "oil on canvas",
            "objectTypeName": "painting",
            "isCopyrighted": False,
            "renditions": {
                "iiif": "https://iiif.micr.io/iiif/2/SK-A-4691/full/1200,/0/default.jpg",
                "thumbnail": "https://iiif.micr.io/iiif/2/SK-A-4691/full/200,/0/default.jpg",
            },
            "thumbnail": {"url": "https://iiif.micr.io/thumb.jpg"},
            "creditLine": "Rijksmuseum, Amsterdam",
        }
    ],
}


def client_for(payload: dict) -> httpx.AsyncClient:
    """Mock transport returning a fixed payload."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


class TestClevelandAdapter:
    def test_source_identity(self):
        provider = ClevelandMuseumProvider()
        assert provider.source_id == "cleveland"
        assert provider.museum_name == "Cleveland Museum of Art"
        assert isinstance(provider, MuseumProvider)

    async def test_discover_translates_payload(self):
        async with client_for(CLEVELAND_PAYLOAD) as http:
            provider = ClevelandMuseumProvider(http)
            page = await provider.discover(page_size=5)

        assert len(page.artworks) == 1
        art = page.artworks[0]
        assert art.canonical_id == "cleveland:1234"
        assert art.title == "The Cranes"
        assert art.artist_name == "Katsushika Hokusai"
        assert art.public_domain is True
        assert art.open_access is True
        assert art.credit_line is not None
        assert art.primary_image is not None
        assert art.primary_image.url.endswith("1234.web.jpg")
        assert art.thumbnail_url is not None
        assert art.museum_object_url == "https://www.clevelandart.org/art/1234"

    async def test_discover_requests_cc0_and_images(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json=CLEVELAND_PAYLOAD)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await ClevelandMuseumProvider(http).discover(page_size=3, offset=6)

        assert captured["cc0"] == "1"
        assert captured["has_image"] == "1"
        assert captured["limit"] == "3"
        assert captured["skip"] == "6"

    async def test_unknown_rights_stay_unknown(self):
        payload = {
            "data": [
                {
                    **CLEVELAND_PAYLOAD["data"][0],
                    "is_public_domain": None,
                    "share_license_status": None,
                }
            ],
            "info": {"total": 1},
        }
        async with client_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover()
        art = page.artworks[0]
        assert art.public_domain is None
        assert art.rights_text is None

    async def test_get_artwork_returns_none_on_404(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404))
        ) as http:
            assert await ClevelandMuseumProvider(http).get_artwork("999") is None

    async def test_pagination_flags(self):
        payload = {
            "data": CLEVELAND_PAYLOAD["data"],
            "info": {"total": 10},
        }
        async with client_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover(page_size=1, offset=2)
        assert page.has_more is True
        assert page.next_offset == 3
        assert page.total_count == 10


class TestRijksmuseumAdapter:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="requires an API key"):
            RijksmuseumProvider("")

    def test_source_identity(self):
        provider = RijksmuseumProvider("key")
        assert provider.source_id == "rijksmuseum"
        assert provider.museum_name == "Rijksmuseum"
        assert isinstance(provider, MuseumProvider)

    async def test_discover_translates_payload(self):
        async with client_for(RIJKS_PAYLOAD) as http:
            provider = RijksmuseumProvider("key", http)
            page = await provider.discover(page_size=5)

        art = page.artworks[0]
        assert art.canonical_id == "rijksmuseum:SK-A-4691"
        assert art.title == "The Milkmaid"
        assert art.artist_name == "Johannes Vermeer"
        assert art.year_start == 1660
        assert art.public_domain is True
        assert art.primary_image is not None
        assert "iiif" in art.primary_image.url
        assert art.thumbnail_url is not None
        assert page.total_count == 137
        assert page.has_more is True

    async def test_api_key_is_sent(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json=RIJKS_PAYLOAD)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await RijksmuseumProvider("secret-key", http).discover(page_size=10)

        assert captured["key"] == "secret-key"
        assert captured["imgonly"] == "True"
        assert captured["ps"] == "10"

    async def test_copyrighted_object_is_marked_restricted(self):
        payload = {
            "count": 1,
            "items": [{**RIJKS_PAYLOAD["items"][0], "isCopyrighted": True}],
        }
        async with client_for(payload) as http:
            page = await RijksmuseumProvider("key", http).discover()
        assert page.artworks[0].public_domain is False

    async def test_unknown_rights_stay_unknown(self):
        payload = {
            "count": 1,
            "items": [{k: v for k, v in RIJKS_PAYLOAD["items"][0].items() if k != "isCopyrighted"}],
        }
        async with client_for(payload) as http:
            page = await RijksmuseumProvider("key", http).discover()
        art = page.artworks[0]
        assert art.public_domain is None, "unknown rights must not be inferred as public domain"

    async def test_record_without_object_number_is_dropped(self):
        payload = {"count": 1, "items": [{"title": "No identifier"}]}
        async with client_for(payload) as http:
            page = await RijksmuseumProvider("key", http).discover()
        assert page.artworks == []

    async def test_offset_maps_to_page_number(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json=RIJKS_PAYLOAD)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await RijksmuseumProvider("key", http).discover(page_size=20, offset=40)

        assert captured["p"] == "3"


class TestRegistryWiring:
    def test_default_registry_contains_keyless_museums(self):
        registry = build_registry(Settings(_env_file=None))  # type: ignore[call-arg]
        assert set(registry.source_ids) == {"artic", "met", "cleveland"}

    def test_rijksmuseum_requires_key(self):
        registry = build_registry(
            Settings(_env_file=None, rijksmuseum_api_key="key")  # type: ignore[call-arg]
        )
        assert "rijksmuseum" in registry.source_ids

    def test_default_providers_mapping(self):
        providers = default_providers(Settings(_env_file=None))  # type: ignore[call-arg]
        assert providers["cleveland"].source_id == "cleveland"
        assert "met" in providers

    def test_registry_lookup(self):
        registry = build_registry(Settings(_env_file=None))  # type: ignore[call-arg]
        assert registry.get("artic") is not None
        assert registry.get("nope") is None
        assert "nope" not in registry
        assert len(registry) == 3


def test_cleveland_base_url_is_https():
    assert CLEVELAND_BASE_URL.startswith("https://")


def test_artwork_requires_identity():
    with pytest.raises(ValueError):
        Artwork(title="x", museum_name="y")  # type: ignore[call-arg]


class TestClevelandFieldShapes:
    """The CMA API returns several fields as lists or nested objects."""

    LIST_SHAPED_PAYLOAD: ClassVar[dict] = {
        "data": [
            {
                **CLEVELAND_PAYLOAD["data"][0],
                "title": ["A Study in Ochre"],
                "culture": ["America"],
                "technique": ["oil on canvas", "panel"],
                "creation_date": ["1912", "1913"],
                "description": ["<p>A study.</p>"],
                "credit_line": ["Gift of the artist"],
                "images": {
                    "web_image": ["https://cdn.test/a.web.jpg"],
                    "thumbnail": ["https://cdn.test/a.thumb.jpg"],
                },
                "department": {"title": ["Paintings"]},
                "type": {"title": ["Painting"]},
                "tags": [{"type": "style", "title": ["Realism"]}],
            }
        ],
        "info": {"total": 1},
    }

    async def test_list_fields_are_flattened(self):
        async with client_for(self.LIST_SHAPED_PAYLOAD) as http:
            page = await ClevelandMuseumProvider(http).discover()

        art = page.artworks[0]
        assert art.title == "A Study in Ochre"
        assert art.culture == "America"
        assert art.medium == "oil on canvas"
        assert art.creation_date_text == "1912"
        assert art.credit_line == "Gift of the artist"
        assert art.department == "Paintings"
        assert art.classification == "Painting"
        assert art.style_raw == "Realism"
        assert art.primary_image is not None
        assert art.primary_image.url == "https://cdn.test/a.web.jpg"

    async def test_non_boolean_public_domain_is_unknown(self):
        payload = {
            "data": [
                {
                    **CLEVELAND_PAYLOAD["data"][0],
                    "is_public_domain": "yes",
                    "share_license_status": None,
                }
            ],
            "info": {"total": 1},
        }
        async with client_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover()
        art = page.artworks[0]
        assert art.public_domain is None
        assert art.rights_text is None

    async def test_empty_fields_do_not_break_translation(self):
        payload = {
            "data": [
                {
                    "id": 99,
                    "title": None,
                    "creators": [],
                    "images": {},
                    "tags": [],
                    "culture": [],
                }
            ],
            "info": {"total": 1},
        }
        async with client_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover()
        art = page.artworks[0]
        assert art.title == "Untitled"
        assert art.artist_name is None
        assert art.primary_image is None
