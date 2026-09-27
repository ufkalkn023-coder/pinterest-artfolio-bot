"""Cleveland Museum of Art adapter.

The CMA Open Access API is free and requires no API key.

API docs: https://openaccess-api.clevelandart.org/
Base URL: https://openaccess-api.clevelandart.org/api/artworks/

Key characteristics:
  - No authentication required
  - Open Access (CC0) images flagged per object via `share_license_status`
  - `is_public_domain` flag on each artwork
  - IIIF-style image service with `web_image` / `print_image` renditions
  - Rich creators, techniques, departments, and classification data
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from httpx import AsyncClient

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.museums.base import DiscoveryPage, MuseumProvider, SearchQuery
from pinterest_art_bot.museums.http import MuseumHttpClient

logger = structlog.get_logger()

CLEVELAND_BASE_URL = "https://openaccess-api.clevelandart.org/api/artworks"
CLEVELAND_ARTWORK_URL = "https://www.clevelandart.org/art"


class ClevelandMuseumProvider(MuseumProvider):
    """Adapter for the Cleveland Museum of Art Open Access API."""

    def __init__(self, http_client: MuseumHttpClient | AsyncClient | None = None) -> None:
        if isinstance(http_client, MuseumHttpClient):
            self._client = http_client
        elif isinstance(http_client, AsyncClient):
            self._client = MuseumHttpClient(client=http_client)
        else:
            self._client = MuseumHttpClient()

    @property
    def source_id(self) -> str:
        return "cleveland"

    @property
    def museum_name(self) -> str:
        return "Cleveland Museum of Art"

    async def discover(
        self,
        *,
        page_size: int = 20,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> DiscoveryPage:
        """Discover open-access artworks with images."""
        params: dict[str, str] = {
            "has_image": "1",
            "cc0": "1",
            "limit": str(page_size),
            "skip": str(int(offset or 0)),
        }
        return await self._fetch_page(params)

    async def get_artwork(self, object_id: str) -> Artwork | None:
        """Fetch a single Cleveland artwork by ID."""
        resp = await self._client.get(f"{CLEVELAND_BASE_URL}/{object_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return self._translate(data.get("data", data))

    async def search(self, query: SearchQuery) -> DiscoveryPage:
        """Search the Cleveland collection."""
        params: dict[str, str] = {
            "limit": str(query.page_size),
            "skip": str(int(query.offset or 0)),
        }
        if query.query:
            params["q"] = query.query
        if query.has_images:
            params["has_image"] = "1"
        if query.public_domain_only:
            params["cc0"] = "1"
        if query.culture:
            params["culture"] = query.culture
        if query.medium:
            params["technique"] = query.medium
        if query.date_start is not None and query.date_end is not None:
            params["created_after"] = str(query.date_start)
            params["created_before"] = str(query.date_end)

        return await self._fetch_page(params)

    async def _fetch_page(self, params: dict[str, str]) -> DiscoveryPage:
        data = await self._client.get_json(CLEVELAND_BASE_URL, params=params)
        items = data.get("data") or []
        info = data.get("info") or {}
        total = info.get("total")

        artworks = [self._translate(item) for item in items if isinstance(item, dict)]
        limit = int(params.get("limit", len(artworks)))
        skip = int(params.get("skip", 0))
        has_more = bool(total is not None and skip + len(artworks) < int(total))

        return DiscoveryPage(
            artworks=artworks,
            total_count=int(total) if total is not None else len(artworks),
            has_more=has_more,
            next_offset=skip + limit if has_more else None,
        )

    def _translate(self, data: dict) -> Artwork:  # type: ignore[type-arg]
        """Translate a Cleveland API artwork into canonical Artwork.

        Field shapes vary across CMA records: ``images`` nests renditions as
        objects (``images.web.url``), some free-text fields are lists, and older
        payloads use flat keys (``web_image``). Everything is normalised here.
        """
        artwork_id = str(data.get("id", ""))
        title = _first_text(data.get("title")) or "Untitled"

        creators = data.get("creators") or []
        artist_name = None
        if creators and isinstance(creators[0], dict):
            artist_name = _first_text(creators[0].get("description"))

        primary = _best_rendition(data.get("images"))
        primary_image = (
            ImageInfo(url=primary.url, width=primary.width, height=primary.height)
            if primary.url
            else None
        )
        thumbnail_url = _thumbnail_url(data.get("images"))

        tags = [t for t in (data.get("tags") or []) if isinstance(t, dict)]
        style_titles: list[str] = []
        for tag in tags:
            if tag.get("type") != "style":
                continue
            style_title = _first_text(tag.get("title"))
            if style_title:
                style_titles.append(style_title)

        # CMA states reuse rights through share_license_status, not a public-domain flag.
        share_license = _first_text(data.get("share_license_status"))
        public_domain_flag = data.get("is_public_domain")
        public_domain = public_domain_flag if isinstance(public_domain_flag, bool) else None
        is_cc0 = (share_license or "").upper() == "CC0"

        return Artwork(
            identity=ArtworkIdentity(source="cleveland", source_object_id=artwork_id),
            title=title,
            artist_name=artist_name,
            creation_date_text=_first_text(data.get("creation_date"))
            or _first_text(data.get("date_text")),
            year_start=_safe_int(data.get("creation_date_earliest")),
            year_end=_safe_int(data.get("creation_date_latest")),
            culture=_first_text(data.get("culture")),
            department=_nested_text(data.get("department")),
            classification=_nested_text(data.get("type")),
            medium=_first_text(data.get("technique")),
            movement_raw=None,
            style_raw=style_titles[0] if style_titles else None,
            tags=style_titles,
            description=_first_text(data.get("description")),
            museum_name="Cleveland Museum of Art",
            museum_object_url=_first_text(data.get("url"))
            or f"{CLEVELAND_ARTWORK_URL}/{artwork_id}",
            primary_image=primary_image,
            thumbnail_url=thumbnail_url,
            public_domain=True if is_cc0 else public_domain,
            open_access=is_cc0 or public_domain is True,
            rights_text="Public domain (CC0)" if is_cc0 else None,
            credit_line=_first_text(data.get("creditline")) or _first_text(data.get("credit_line")),
            metadata_raw=data,
        )


@dataclass(frozen=True)
class _Rendition:
    """A CMA image rendition."""

    url: str | None = None
    width: int | None = None
    height: int | None = None


def _best_rendition(images: object) -> _Rendition:
    """Pick the best rendition for publishing.

    Order: print → web → full → legacy flat keys. CMA's ``full`` rendition is a
    multi-megabyte TIFF that exceeds the media size ceiling, so it is only a last
    resort; ``print`` and ``web`` are publishable JPEGs.
    """
    if not isinstance(images, dict):
        return _Rendition(url=_first_text(images))

    for key in ("print", "web", "full"):
        node = images.get(key)
        if isinstance(node, dict) and _first_text(node.get("url")):
            return _Rendition(
                url=_first_text(node.get("url")),
                width=_safe_int(node.get("width")),
                height=_safe_int(node.get("height")),
            )

    for legacy_key in ("web_image", "print_image", "full_image", "url"):
        url = _first_text(images.get(legacy_key))
        if url:
            return _Rendition(url=url)
    return _Rendition()


def _thumbnail_url(images: object) -> str | None:
    """Thumbnail URL, preferring a small explicit rendition."""
    if not isinstance(images, dict):
        return None
    for key in ("thumbnail", "web", "print"):
        node = images.get(key)
        url = _first_text(node.get("url")) if isinstance(node, dict) else _first_text(node)
        if url:
            return url
    return None


def _nested_text(value: object) -> str | None:
    """Read a text field that may be a string or an object with a title/value."""
    if isinstance(value, dict):
        return _first_text(value.get("title")) or _first_text(value.get("name"))
    return _first_text(value)


def _safe_int(value: object) -> int | None:
    """Safely convert a CMA numeric string to int."""
    text = _first_text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return None


def _first_text(value: object) -> str | None:
    """Flatten a CMA field that may be a string, a list, or a nested object.

    The API returns some fields as lists (e.g. culture: ["America"]) and others as
    objects, so every free-text field is normalised through here.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        for entry in value:
            text = _first_text(entry)
            if text:
                return text
        return None
    if isinstance(value, dict):
        for key in ("title", "value", "name", "description", "url"):
            text = _first_text(value.get(key))
            if text:
                return text
    return None
