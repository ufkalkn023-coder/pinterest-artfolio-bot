"""Additional coverage for the Met adapter and the museum HTTP client.

The Met adapter is the most-used museum source, so its discovery, search,
pagination, and rights-translation behaviour is pinned here.
"""

import httpx
import pytest

from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.museums.http import (
    MuseumApiError,
    MuseumHttpClient,
    MuseumRateLimitError,
    MuseumTimeoutError,
)

OBJECT_PAYLOAD = {
    "objectID": 436535,
    "title": "Wheat Field with Cypresses",
    "artistDisplayName": "Vincent van Gogh",
    "artistULAN_URL": "https://ulan.mfa.org/artist/500010800",
    "objectDate": "1889",
    "objectBeginDate": 1889,
    "objectEndDate": 1889,
    "culture": "Dutch",
    "department": "European Paintings",
    "classification": "Paintings",
    "objectName": "Painting",
    "medium": "Oil on canvas",
    "period": "Post-Impression",
    "primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg",
    "primaryImageSmall": "https://images.metmuseum.org/CRDImages/ep/web-large/DT1567.jpg",
    "additionalImages": ["https://images.metmuseum.org/extra1.jpg", ""],
    "isPublicDomain": True,
    "rightsAndReproduction": "Public Domain",
    "creditLine": "Purchase, The Annenberg Foundation Gift, 1993",
    "objectURL": "https://www.metmuseum.org/art/collection/search/436535",
    "tags": [{"term": "Cypresses"}, {"term": "Fields"}, "legacy-string-tag"],
}


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def responder(payload: dict, calls: list | None = None):
    """Return a request handler serving a fixed payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(dict(request.url.params))
        return httpx.Response(200, json=payload)

    return handler


class TestMetTranslation:
    async def test_full_translation(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        async with transport(responder(OBJECT_PAYLOAD)) as http:
            artwork = await MetMuseumProvider(http).get_artwork("436535")

        assert artwork is not None
        assert artwork.canonical_id == "met:436535"
        assert artwork.artist_name == "Vincent van Gogh"
        assert artwork.artist_id == "500010800"
        assert artwork.year_start == 1889
        assert artwork.public_domain is True
        assert artwork.open_access is True
        assert artwork.rights_text == "Public Domain"
        assert artwork.period_raw == "Post-Impression"
        assert artwork.tags == ["Cypresses", "Fields", "legacy-string-tag"]
        assert artwork.primary_image is not None
        assert len(artwork.additional_images) == 1  # empty URLs are dropped
        assert artwork.thumbnail_url is not None

    async def test_missing_object_returns_none(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        async with transport(lambda request: httpx.Response(404)) as http:
            assert await MetMuseumProvider(http).get_artwork("1") is None

    async def test_missing_optional_fields_are_tolerated(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        minimal = {"objectID": 1, "title": "Untitled thing"}
        async with transport(responder(minimal)) as http:
            artwork = await MetMuseumProvider(http).get_artwork("1")

        assert artwork is not None
        assert artwork.artist_name is None
        assert artwork.primary_image is None
        assert artwork.public_domain is None
        assert artwork.rights_text is None

    async def test_non_public_domain_object(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        payload = {**OBJECT_PAYLOAD, "isPublicDomain": False}
        async with transport(responder(payload)) as http:
            artwork = await MetMuseumProvider(http).get_artwork("436535")
        assert artwork is not None
        assert artwork.public_domain is False


class TestMetDiscovery:
    async def test_discover_prefers_public_domain_with_image(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        objects = {
            "1": {**OBJECT_PAYLOAD, "objectID": 1, "isPublicDomain": True, "primaryImage": "u1"},
            "2": {**OBJECT_PAYLOAD, "objectID": 2, "isPublicDomain": False, "primaryImage": "u2"},
            "3": {**OBJECT_PAYLOAD, "objectID": 3, "isPublicDomain": True, "primaryImage": ""},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"objectIDs": [1, 2, 3], "total": 3})
            obj_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=objects[obj_id])

        async with transport(handler) as http:
            page = await MetMuseumProvider(http).discover(page_size=3)

        assert page.total_count == 3
        ids = [a.identity.source_object_id for a in page.artworks]
        assert "1" in ids
        # The non-public-domain object is only used as a fallback while under quota.
        assert page.artworks[0].public_domain is True

    async def test_discover_reports_more_pages(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"objectIDs": list(range(1, 11)), "total": 10})
            obj_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={**OBJECT_PAYLOAD, "objectID": int(obj_id), "isPublicDomain": True},
            )

        async with transport(handler) as http:
            page = await MetMuseumProvider(http).discover(page_size=2, offset=0)

        assert page.has_more is True
        assert page.next_offset is not None

    async def test_search_passes_filters(self):
        from pinterest_art_bot.museums.base import SearchQuery
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                calls.append(dict(request.url.params))
                return httpx.Response(200, json={"objectIDs": [436535], "total": 1})
            return httpx.Response(200, json=OBJECT_PAYLOAD)

        async with transport(handler) as http:
            page = await MetMuseumProvider(http).search(
                SearchQuery(
                    query="van gogh",
                    public_domain_only=True,
                    has_images=True,
                    page_size=5,
                )
            )

        assert page.artworks[0].canonical_id == "met:436535"
        assert calls[0]["q"] == "van gogh"
        assert calls[0]["isPublicDomain"] == "true"
        assert calls[0]["hasImages"] == "true"

    async def test_search_without_query_uses_wildcard(self):
        from pinterest_art_bot.museums.base import SearchQuery
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                calls.append(dict(request.url.params))
                return httpx.Response(200, json={"objectIDs": [], "total": 0})
            return httpx.Response(200, json=OBJECT_PAYLOAD)

        async with transport(handler) as http:
            await MetMuseumProvider(http).search(SearchQuery())

        assert calls[0]["q"] == "*"


class TestMuseumHttpClientBehaviour:
    async def test_retry_after_header_is_honoured(self, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(delay):
            slept.append(delay)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"ok": True})

        async with transport(handler) as http:
            data = await MuseumHttpClient(client=http).get_json("https://api.test/x")

        assert data == {"ok": True}
        assert slept == [2.0]

    async def test_invalid_json_raises_api_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        async with transport(handler) as http:
            with pytest.raises(MuseumApiError, match="Failed to parse JSON"):
                await MuseumHttpClient(client=http).get_json("https://api.test/x")

    async def test_rate_limit_raises_after_retries(self, monkeypatch):
        async def fake_sleep(delay):
            return None

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        async with transport(handler) as http:
            with pytest.raises(MuseumRateLimitError):
                await MuseumHttpClient(client=http, max_retries=1).get("https://api.test/x")

    async def test_timeout_raises_after_retries(self, monkeypatch):
        async def fake_sleep(delay):
            return None

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        async with transport(handler) as http:
            with pytest.raises(MuseumTimeoutError):
                await MuseumHttpClient(client=http, max_retries=1).get("https://api.test/x")

    async def test_client_owns_and_closes_its_session(self):
        client = MuseumHttpClient()
        assert client._owns_client is True

    def test_injected_client_is_not_closed(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        client = MuseumHttpClient(client=http)
        assert client._owns_client is False


def test_safe_int_helper():
    from pinterest_art_bot.museums.met.adapter import _safe_int

    assert _safe_int("1889") == 1889
    assert _safe_int(1889.0) == 1889
    assert _safe_int(None) is None
    assert _safe_int("not a number") is None
    assert _safe_int({"a": 1}) is None  # type: ignore[arg-type]


def test_artwork_fingerprint_is_deterministic():
    from pinterest_art_bot.domain.artwork import ArtworkIdentity

    artwork = Artwork(
        identity=ArtworkIdentity(source="met", source_object_id="1"),
        title="Title",
        artist_name="Artist",
        museum_name="Met",
    )
    assert artwork.compute_metadata_fingerprint() == artwork.compute_metadata_fingerprint()
    assert Artwork.compute_image_sha256(b"x") == Artwork.compute_image_hash(b"x")


class TestMalformedPayloads:
    """A 200 response with the wrong shape must fail loudly, not with a KeyError."""

    async def test_get_artwork_requires_object_id(self):
        from pinterest_art_bot.museums.http import MuseumApiError
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        async with transport(lambda request: httpx.Response(200, json={"pagination": {}})) as http:
            with pytest.raises(MuseumApiError, match="without objectID"):
                await MetMuseumProvider(http).get_artwork("436535")

    async def test_bulk_discovery_skips_unusable_records(self):
        from pinterest_art_bot.museums.met.adapter import MetMuseumProvider

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"objectIDs": [1, 2, 3], "total": 3})
            obj_id = request.url.path.rsplit("/", 1)[-1]
            if obj_id == "2":
                return httpx.Response(200, json={"unexpected": "shape"})
            return httpx.Response(
                200,
                json={**OBJECT_PAYLOAD, "objectID": int(obj_id), "isPublicDomain": True},
            )

        async with transport(handler) as http:
            page = await MetMuseumProvider(http).discover(page_size=5)

        assert [a.identity.source_object_id for a in page.artworks] == ["1", "3"]
