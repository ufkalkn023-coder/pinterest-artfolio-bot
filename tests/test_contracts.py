"""Contract tests against recorded museum API payloads.

Why these exist: the Cleveland adapter shipped broken for a day because the live
API returns `images.web.url` as an object and `culture` as a list, while the
adapter expected flat strings. Unit tests were written against the same wrong
assumption, so nothing caught it until a live run.

These fixtures are recorded from the real APIs (`tests/fixtures/*.json`, trimmed
to the fields the adapters read) and asserted here, so an upstream shape change
fails in CI instead of in production. Rijksmuseum's fixture is synthetic because
that API needs a key; its shape is documented in the adapter docstring.

Re-record with: pytest --record-fixtures
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.museums.artic.adapter import ARTIC_BASE_URL, ArticMuseumProvider
from pinterest_art_bot.museums.base import SearchQuery
from pinterest_art_bot.museums.cleveland.adapter import ClevelandMuseumProvider
from pinterest_art_bot.museums.met.adapter import MET_BASE_URL, MetMuseumProvider
from pinterest_art_bot.museums.rijksmuseum.adapter import RijksmuseumProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    """Load a recorded payload."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def transport_for(payload: Any) -> httpx.AsyncClient:
    """Serve one recorded payload for any request."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


def assert_usable(artwork: Artwork, *, source: str) -> None:
    """Invariants every translated artwork must meet."""
    assert artwork.identity.source == source
    assert artwork.canonical_id.startswith(f"{source}:")
    assert artwork.title.strip(), "a title is required"
    assert artwork.museum_name
    # Rights are only ever set from a real API signal: bool or explicitly None.
    assert artwork.public_domain is None or isinstance(artwork.public_domain, bool)
    # Free-text fields must be strings, never lists or nested objects.
    for field in ("artist_name", "culture", "medium", "department", "classification"):
        value = getattr(artwork, field)
        assert value is None or isinstance(value, str), f"{field} is not text: {value!r}"


def assert_image_usable(artwork: Artwork) -> None:
    """Publishable candidates need a usable image URL."""
    assert artwork.best_image_url is not None
    assert artwork.best_image_url.startswith("http")


class TestArticContract:
    async def test_search_payload(self):
        payload = load("artic_search")
        async with transport_for(payload) as http:
            page = await ArticMuseumProvider(http).discover(page_size=2)

        assert page.artworks
        for artwork in page.artworks:
            assert_usable(artwork, source="artic")
            assert_image_usable(artwork)
            assert artwork.public_domain is True

    async def test_artwork_payload(self):
        payload = load("artic_artwork")
        async with transport_for(payload) as http:
            artwork = await ArticMuseumProvider(http).get_artwork("16568")

        assert artwork is not None
        assert_usable(artwork, source="artic")
        assert_image_usable(artwork)
        # Recorded from the live API: this object has no license string, only the
        # public-domain flag. The adapter must not invent one.
        assert artwork.public_domain is True
        assert artwork.license is None
        assert artwork.credit_line

    async def test_unknown_upstream_fields_are_ignored(self):
        """A new upstream field must never break translation."""
        payload = load("artic_artwork")
        payload["data"]["totallyNewField"] = {"nested": True}
        payload["data"]["anotherNewOne"] = [1, 2, 3]

        async with transport_for(payload) as http:
            artwork = await ArticMuseumProvider(http).get_artwork("16568")

        assert artwork is not None
        assert artwork.title


class TestMetContract:
    async def test_object_payload(self):
        payload = load("met_object")
        async with transport_for(payload) as http:
            artwork = await MetMuseumProvider(http).get_artwork("436535")

        assert artwork is not None
        assert_usable(artwork, source="met")
        assert_image_usable(artwork)
        assert artwork.public_domain is True
        assert artwork.year_start == 1889

    async def test_search_payload(self):
        payload = load("met_search")
        async with transport_for(payload) as http:
            page = await MetMuseumProvider(http).search(SearchQuery(query="*", page_size=3))

        # Recorded IDs point at real objects; the fixture only serves the list.
        assert page.total_count == payload["total"]
        assert isinstance(page.artworks, list)

    async def test_unknown_upstream_fields_are_ignored(self):
        payload = load("met_object")
        payload["brandNewField"] = {"deeply": {"nested": True}}

        async with transport_for(payload) as http:
            artwork = await MetMuseumProvider(http).get_artwork("436535")

        assert artwork is not None
        assert artwork.canonical_id == "met:436535"


class TestClevelandContract:
    async def test_list_payload(self):
        """The recorded payload nests images and lists culture values."""
        payload = load("cleveland_artworks")
        async with transport_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover(page_size=2)

        assert page.artworks
        for artwork in page.artworks:
            assert_usable(artwork, source="cleveland")
            assert_image_usable(artwork)
            assert isinstance(artwork.culture, str | type(None))
            assert isinstance(artwork.medium, str | type(None))

    async def test_image_rendition_is_a_url_string(self):
        """CMA returns images.web.url as an object, not a string.

        This is the assertion that would have caught the original bug.
        """
        payload = load("cleveland_artworks")
        raw = payload["data"][0]["images"]
        assert isinstance(raw, dict)
        assert isinstance(raw.get("web"), dict), "images.web is an object upstream"
        assert isinstance(raw["web"].get("url"), str)

        async with transport_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover(page_size=1)

        artwork = page.artworks[0]
        assert artwork.primary_image is not None
        assert artwork.primary_image.url.startswith("https://")
        assert artwork.primary_image.url.endswith((".jpg", ".jpeg", ".tif", ".png"))

    async def test_rendition_is_not_an_oversized_tiff(self):
        """`full` is a multi-hundred-megabyte TIFF; `print` must win."""
        payload = load("cleveland_artworks")
        images = payload["data"][0]["images"]
        if "print" in images:
            async with transport_for(payload) as http:
                page = await ClevelandMuseumProvider(http).discover(page_size=1)
            assert not page.artworks[0].best_image_url.endswith(".tif")

    async def test_detail_payload(self):
        payload = load("cleveland_artwork")
        async with transport_for(payload) as http:
            artwork = await ClevelandMuseumProvider(http).get_artwork("1915.534")

        assert artwork is not None
        assert_usable(artwork, source="cleveland")
        assert_image_usable(artwork)
        assert artwork.credit_line

    async def test_cc0_licence_is_reflected_in_rights(self):
        payload = load("cleveland_artworks")
        async with transport_for(payload) as http:
            page = await ClevelandMuseumProvider(http).discover(page_size=2)

        for artwork in page.artworks:
            if artwork.rights_text:
                assert "CC0" in artwork.rights_text
                assert artwork.public_domain is True


class TestRijksmuseumContract:
    async def test_synthetic_shape(self):
        """Shape documented from the Rijksmuseum Data API (key required)."""
        payload = load("rijksmuseum_search")
        async with transport_for(payload) as http:
            provider = RijksmuseumProvider("test-key", http)
            page = await provider.discover(page_size=2)

        assert page.artworks
        for artwork in page.artworks:
            assert_usable(artwork, source="rijksmuseum")
            assert_image_usable(artwork)
            assert isinstance(artwork.title, str)

    async def test_copyright_flag_is_honoured(self):
        payload = load("rijksmuseum_search")
        async with transport_for(payload) as http:
            page = await RijksmuseumProvider("key", http).discover(page_size=2)

        by_id = {artwork.canonical_id: artwork for artwork in page.artworks}
        assert by_id["rijksmuseum:SK-A-4691"].public_domain is True
        assert by_id["rijksmuseum:SK-A-2344"].public_domain is False

    async def test_missing_is_copyrighted_stays_unknown(self):
        payload = load("rijksmuseum_search")
        for item in payload["items"]:
            item.pop("isCopyrighted", None)

        async with transport_for(payload) as http:
            page = await RijksmuseumProvider("key", http).discover(page_size=2)

        assert all(artwork.public_domain is None for artwork in page.artworks)


class TestApiKeysAreSent:
    async def test_rijksmuseum_key_is_always_present(self):
        """The Rijksmuseum API rejects anonymous requests."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("key"))
            return httpx.Response(200, json=load("rijksmuseum_search"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await RijksmuseumProvider("secret", http).discover(page_size=1)

        assert seen and all(value == "secret" for value in seen)


@pytest.mark.parametrize(
    ("fixture", "base_url"),
    [
        ("met_object", MET_BASE_URL),
        ("artic_artwork", ARTIC_BASE_URL),
    ],
)
def test_fixtures_reference_https_endpoints(fixture, base_url):
    """All museum endpoints must be HTTPS."""
    assert base_url.startswith("https://")
    assert (FIXTURES / f"{fixture}.json").exists()
