"""Rijksmuseum adapter.

The Rijksmuseum Data API requires a free API key (Rijksmuseum API key).

API docs: https://data.rijksmuseum.nl/
Base URL: https://data.rijksmuseum.nl

Key characteristics:
  - REQUIRES an API key (`key` query parameter). Without it, the adapter refuses
    to start rather than issuing doomed requests.
  - Two-step discovery: search returns object numbers, then object detail.
  - `isCopyrighted` is the explicit rights signal; anything else is UNKNOWN and
    therefore denied by the rights gate.
  - IIIF renditions via `iiif` link plus `thumbnail` metadata.
"""

from __future__ import annotations

import structlog
from httpx import AsyncClient

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.museums.base import DiscoveryPage, MuseumProvider, SearchQuery
from pinterest_art_bot.museums.http import MuseumApiError, MuseumHttpClient

logger = structlog.get_logger()

RIJKS_BASE_URL = "https://data.rijksmuseum.nl"
RIJKS_ARTWORK_URL = "https://www.rijksmuseum.nl/en/collection"


class RijksmuseumProvider(MuseumProvider):
    """Adapter for the Rijksmuseum Data API."""

    def __init__(
        self,
        api_key: str,
        http_client: MuseumHttpClient | AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError(
                "RijksmuseumProvider requires an API key (RIJKSMUSEUM_API_KEY). "
                "The Rijksmuseum Data API rejects anonymous requests."
            )
        self._api_key = api_key
        if isinstance(http_client, MuseumHttpClient):
            self._client = http_client
        elif isinstance(http_client, AsyncClient):
            self._client = MuseumHttpClient(client=http_client)
        else:
            self._client = MuseumHttpClient()

    @property
    def source_id(self) -> str:
        return "rijksmuseum"

    @property
    def museum_name(self) -> str:
        return "Rijksmuseum"

    async def discover(
        self,
        *,
        page_size: int = 20,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> DiscoveryPage:
        """Discover public-domain objects with images."""
        params: dict[str, str] = {
            "key": self._api_key,
            "imgonly": "True",
            "ps": str(page_size),
            "p": str(self._page_number(offset, page_size)),
        }
        return await self._search_page(params)

    async def get_artwork(self, object_id: str) -> Artwork | None:
        """Fetch a single Rijksmuseum object detail record."""
        data = await self._client.get_json(
            f"{RIJKS_BASE_URL}/collection/{object_id}",
            params={"key": self._api_key, "imgonly": "True"},
        )
        record = data.get("record") or data
        if not isinstance(record, dict) or not record.get("objectNumber"):
            return None
        return self._translate(record)

    async def search(self, query: SearchQuery) -> DiscoveryPage:
        """Search the Rijksmuseum collection."""
        params: dict[str, str] = {
            "key": self._api_key,
            "ps": str(query.page_size),
            "p": str(self._page_number(query.offset, query.page_size)),
        }
        if query.query:
            params["q"] = query.query
        if query.has_images:
            params["imgonly"] = "True"
        if query.culture:
            params["f.classification"] = query.culture

        return await self._search_page(params)

    @staticmethod
    def _page_number(offset: int | None, page_size: int) -> int:
        """Translate an offset into a 1-based page number."""
        if page_size <= 0:
            return 1
        return (int(offset or 0) // page_size) + 1

    async def _search_page(self, params: dict[str, str]) -> DiscoveryPage:
        data = await self._client.get_json(f"{RIJKS_BASE_URL}/search/collection", params=params)
        records = data.get("items") or data.get("collection") or []
        if not isinstance(records, list):
            raise MuseumApiError("Rijksmuseum search returned an unexpected payload shape.")

        artworks: list[Artwork] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            artwork = self._translate(record)
            if artwork is not None:
                artworks.append(artwork)

        total = data.get("count")
        page = int(params.get("p", 1))
        page_size = int(params.get("ps", len(artworks)))
        has_more = bool(total is not None and page * page_size < int(total))

        return DiscoveryPage(
            artworks=artworks,
            total_count=int(total) if total is not None else len(artworks),
            has_more=has_more,
            next_offset=page * page_size if has_more else None,
        )

    def _translate(self, data: dict) -> Artwork | None:  # type: ignore[type-arg]
        """Translate a Rijksmuseum record into canonical Artwork.

        Returns None when the record carries no object number, since identity is
        required and must never be guessed.
        """
        object_number = data.get("objectNumber")
        if not object_number:
            return None

        title = _pick_text(data.get("title"))
        artist_name = _first_creator_name(data)

        image_url = _pick_iiif_url(data)
        primary_image = ImageInfo(url=image_url) if image_url else None
        thumbnail_url = _pick_thumbnail(data)

        # Rijksmuseum states rights explicitly via isCopyrighted.
        is_copyrighted = data.get("isCopyrighted")
        if is_copyrighted is True:
            public_domain: bool | None = False
        elif is_copyrighted is False:
            public_domain = True
        else:
            # Unknown rights stay UNKNOWN — the rights gate denies by default.
            public_domain = None

        return Artwork(
            identity=ArtworkIdentity(
                source="rijksmuseum",
                source_object_id=str(object_number),
            ),
            title=title or "Untitled",
            artist_name=artist_name,
            creation_date_text=_pick_text(data.get("dating")),
            year_start=_safe_int(data.get("yearFrom")),
            year_end=_safe_int(data.get("yearTo")),
            culture=_pick_text(data.get("classification")),
            medium=_pick_text(data.get("medium")),
            object_type=_pick_text(data.get("objectTypeName")),
            description=_pick_text(data.get("description")),
            museum_name="Rijksmuseum",
            museum_object_url=f"{RIJKS_ARTWORK_URL}/{object_number}",
            primary_image=primary_image,
            thumbnail_url=thumbnail_url,
            public_domain=public_domain,
            open_access=public_domain is True,
            rights_text=data.get("rights") if isinstance(data.get("rights"), str) else None,
            credit_line=_pick_text(data.get("creditLine")),
            metadata_raw=data,
        )


def _pick_text(value: object) -> str | None:
    """Flatten a Rijksmuseum multilingual field into plain text."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry:
                return entry
            if isinstance(entry, dict):
                for key in ("value", "en", "nl"):
                    candidate = entry.get(key)
                    if isinstance(candidate, str) and candidate:
                        return candidate
    if isinstance(value, dict):
        for key in ("value", "en", "nl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _first_creator_name(data: dict) -> str | None:  # type: ignore[type-arg]
    """Extract the first linked artist name from a Rijksmuseum record."""
    for entry in data.get("principalMakers") or []:
        if isinstance(entry, dict) and entry.get("name"):
            return str(entry["name"])
        if isinstance(entry, dict) and entry.get("label"):
            return str(entry["label"])
    return _pick_text(data.get("artist"))


def _pick_iiif_url(data: dict) -> str | None:  # type: ignore[type-arg]
    """Prefer the largest IIIF rendition advertised by the record."""
    renditions = data.get("renditions") or {}
    if not isinstance(renditions, dict):
        return None
    for key in ("iiif", "zoom", "large", "screen"):
        candidate = renditions.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _pick_thumbnail(data: dict) -> str | None:  # type: ignore[type-arg]
    """Extract a thumbnail URL from the record metadata."""
    thumbnail = data.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return thumbnail
    if isinstance(thumbnail, dict):
        url = thumbnail.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _safe_int(value: object) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, str, float, bytes)):
            return int(value)
        return None
    except (ValueError, TypeError):
        return None
