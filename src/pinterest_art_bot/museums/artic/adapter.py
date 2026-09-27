"""Art Institute of Chicago adapter.

ARTIC's API is free, requires no API key, and provides excellent
metadata with IIIF image support.

API docs: https://api.artic.edu/docs/
Base URL: https://api.artic.edu/api/v1

Key characteristics:
  - No authentication required
  - is_public_domain flag
  - IIIF image URLs via config.iiif_url
  - Rich classification: style_titles, classification_titles, category_titles
  - Elasticsearch-based search with pagination
  - Has explicit style_title and movement fields (rare and valuable)
"""

from __future__ import annotations

import structlog
from httpx import AsyncClient

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.museums.base import DiscoveryPage, MuseumProvider, SearchQuery
from pinterest_art_bot.museums.http import MuseumHttpClient

logger = structlog.get_logger()

ARTIC_BASE_URL = "https://api.artic.edu/api/v1"
ARTIC_IIIF_BASE = "https://www.artic.edu/iiif/2"


class ArticMuseumProvider(MuseumProvider):
    """Adapter for the Art Institute of Chicago API."""

    def __init__(self, http_client: MuseumHttpClient | AsyncClient | None = None) -> None:
        if isinstance(http_client, MuseumHttpClient):
            self._client = http_client
        elif isinstance(http_client, AsyncClient):
            self._client = MuseumHttpClient(client=http_client)
        else:
            self._client = MuseumHttpClient()

    @property
    def source_id(self) -> str:
        return "artic"

    @property
    def museum_name(self) -> str:
        return "Art Institute of Chicago"

    async def discover(
        self,
        *,
        page_size: int = 20,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> DiscoveryPage:
        """Discover public-domain artworks with images."""
        start = int(offset or 0)
        page = (start // page_size) + 1

        data = await self._client.get_json(
            f"{ARTIC_BASE_URL}/artworks/search",
            params={
                "query[term][is_public_domain]": "true",
                "fields": _FIELDS,
                "limit": str(page_size),
                "page": str(page),
            },
        )

        config = data.get("config", {})
        # Filter for items with a usable image_id
        items = data.get("data", [])
        artworks = [self._translate(item, config) for item in items if item.get("image_id")]

        pagination = data.get("pagination", {})
        total = pagination.get("total", len(artworks))
        current_page = pagination.get("current_page", page)
        total_pages = pagination.get("total_pages", 1)
        has_more = current_page < total_pages

        return DiscoveryPage(
            artworks=artworks,
            total_count=total,
            has_more=has_more,
            # Continue from where this page actually started, not from page 1.
            next_offset=_next_offset(start, len(items), has_more),
        )

    async def get_artwork(self, object_id: str) -> Artwork | None:
        """Fetch a single ARTIC artwork by ID."""
        resp = await self._client.get(
            f"{ARTIC_BASE_URL}/artworks/{object_id}",
            params={"fields": _FIELDS},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()

        return self._translate(data["data"], data.get("config", {}))

    async def search(self, query: SearchQuery) -> DiscoveryPage:
        """Search the ARTIC collection."""
        params: dict[str, str] = {
            "fields": _FIELDS,
            "limit": str(query.page_size),
        }
        if query.query:
            params["q"] = query.query
        if query.public_domain_only:
            params["query[term][is_public_domain]"] = "true"

        page = ((query.offset or 0) // query.page_size) + 1
        params["page"] = str(page)

        data = await self._client.get_json(f"{ARTIC_BASE_URL}/artworks/search", params=params)
        config = data.get("config", {})
        items = data.get("data", [])
        artworks = [self._translate(item, config) for item in items if item.get("image_id")]

        pagination = data.get("pagination", {})
        total = pagination.get("total", len(artworks))
        current_page = pagination.get("current_page", page)
        total_pages = pagination.get("total_pages", 1)
        has_more = current_page < total_pages

        return DiscoveryPage(
            artworks=artworks,
            total_count=total,
            has_more=has_more,
            next_offset=_next_offset(int(query.offset or 0), len(items), has_more),
        )

    def _translate(self, data: dict, config: dict) -> Artwork:  # type: ignore[type-arg]
        """Translate an ARTIC API artwork into canonical Artwork."""
        # Build IIIF image URL
        image_id = data.get("image_id")
        iiif_base = config.get("iiif_url", ARTIC_IIIF_BASE)
        primary_image = None
        thumbnail_url = None

        if image_id:
            full_url = f"{iiif_base}/{image_id}/full/843,/0/default.jpg"
            thumb = data.get("thumbnail")
            thumb_w = thumb.get("width") if isinstance(thumb, dict) else None
            thumb_h = thumb.get("height") if isinstance(thumb, dict) else None
            primary_image = ImageInfo(
                url=full_url,
                width=thumb_w,
                height=thumb_h,
            )
            thumbnail_url = f"{iiif_base}/{image_id}/full/200,/0/default.jpg"

        # Extract style/movement from ARTIC's array fields
        style_titles = data.get("style_titles") or []
        classification_titles = data.get("classification_titles") or []
        category_titles = data.get("category_titles") or []

        # ARTIC sometimes has explicit style_title (singular)
        style_raw = data.get("style_title") or (style_titles[0] if style_titles else None)

        return Artwork(
            identity=ArtworkIdentity(
                source="artic",
                source_object_id=str(data["id"]),
            ),
            title=data.get("title", "Untitled"),
            artist_name=data.get("artist_display") or None,
            artist_id=str(data["artist_id"]) if data.get("artist_id") else None,
            creation_date_text=data.get("date_display") or None,
            year_start=data.get("date_start"),
            year_end=data.get("date_end"),
            culture=None,
            department=data.get("department_title") or None,
            classification=classification_titles[0] if classification_titles else None,
            object_type=data.get("artwork_type_title") or None,
            medium=data.get("medium_display") or None,
            movement_raw=None,  # Derived from style_titles
            period_raw=None,
            style_raw=style_raw,
            tags=style_titles + category_titles,
            description=data.get("description") or None,
            museum_name="Art Institute of Chicago",
            museum_object_url=f"https://www.artic.edu/artworks/{data['id']}",
            primary_image=primary_image,
            thumbnail_url=thumbnail_url,
            public_domain=data.get("is_public_domain"),
            open_access=data.get("is_public_domain"),
            rights_text=None,
            license=data.get("license_text") or None,
            credit_line=data.get("credit_line") or None,
            metadata_raw=data,
        )


def _next_offset(start: int, returned: int, has_more: bool) -> int | None:
    """Offset to request next, derived from this page's actual position."""
    if not has_more:
        return None
    return start + max(returned, 1)


# Fields to request from the ARTIC API
_FIELDS = ",".join(
    [
        "id",
        "title",
        "artist_display",
        "artist_id",
        "date_display",
        "date_start",
        "date_end",
        "department_title",
        "classification_titles",
        "artwork_type_title",
        "medium_display",
        "style_title",
        "style_titles",
        "category_titles",
        "description",
        "image_id",
        "thumbnail",
        "is_public_domain",
        "license_text",
        "credit_line",
    ]
)
