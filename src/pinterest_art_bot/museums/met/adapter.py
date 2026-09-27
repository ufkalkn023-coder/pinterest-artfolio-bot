"""Metropolitan Museum of Art adapter.

The Met's Open Access API is free, requires no API key, and provides
excellent public-domain metadata.

API docs: https://metmuseum.github.io/
Base URL: https://collectionapi.metmuseum.org/public/collection/v1

Key characteristics:
  - No authentication required
  - isPublicDomain flag on each object
  - primaryImage / primaryImageSmall URLs
  - Rich metadata: department, culture, period, dynasty, objectDate
  - Two-step discovery: search → list of IDs, then fetch each object
"""

from __future__ import annotations

import structlog
from httpx import AsyncClient

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.museums.base import DiscoveryPage, MuseumProvider, SearchQuery
from pinterest_art_bot.museums.http import MuseumApiError, MuseumHttpClient

logger = structlog.get_logger()

MET_BASE_URL = "https://collectionapi.metmuseum.org/public/collection/v1"


class MetMuseumProvider(MuseumProvider):
    """Adapter for the Metropolitan Museum of Art Open Access API."""

    def __init__(self, http_client: MuseumHttpClient | AsyncClient | None = None) -> None:
        if isinstance(http_client, MuseumHttpClient):
            self._client = http_client
        elif isinstance(http_client, AsyncClient):
            self._client = MuseumHttpClient(client=http_client)
        else:
            self._client = MuseumHttpClient()
        self._painting_departments: list[str] | None = None

    @property
    def source_id(self) -> str:
        return "met"

    @property
    def museum_name(self) -> str:
        return "The Metropolitan Museum of Art"

    async def discover(
        self,
        *,
        page_size: int = 20,
        cursor: str | None = None,
        offset: int | None = None,
    ) -> DiscoveryPage:
        """Discover public-domain artworks with images and movement evidence.

        The Met's highlight feed mixes paintings with instruments and decorative
        arts, which carry no movement metadata at all. Discovery therefore walks
        the museum's painting/print/photograph departments — resolved from the
        API rather than hardcoded — and skips records without any classification
        signal, so downstream classification has evidence to work with instead
        of a date guess.
        """
        object_ids, total = await self._collect_candidate_ids(page_size, offset)

        start = int(offset or 0)
        artworks: list[Artwork] = []
        current_idx = start

        # Scan objects until we reach page_size or exhaust the list.
        while current_idx < len(object_ids) and len(artworks) < page_size:
            obj_id = object_ids[current_idx]
            current_idx += 1
            artwork = await self._fetch_candidate(str(obj_id))
            if artwork is not None and _is_publishable_candidate(artwork):
                artworks.append(artwork)

        return DiscoveryPage(
            artworks=artworks,
            total_count=total,
            has_more=current_idx < len(object_ids),
            next_offset=current_idx if current_idx < len(object_ids) else None,
        )

    async def _collect_candidate_ids(
        self,
        page_size: int,
        offset: int | None,
    ) -> tuple[list[int], int]:
        """Gather object IDs to scan, from painting departments or highlights."""
        departments = await self._painting_department_ids()
        start = int(offset or 0)
        want = start + page_size * 3  # over-fetch: most records are filtered out

        if not departments:
            data = await self._client.get_json(
                f"{MET_BASE_URL}/search",
                params={"isHighlight": "true", "hasImages": "true", "q": "*"},
            )
            ids = [int(i) for i in (data.get("objectIDs") or [])]
            return ids, int(data.get("total", len(ids)))

        object_ids: list[int] = []
        total = 0
        for department_id in departments:
            if len(object_ids) >= want:
                break
            data = await self._client.get_json(
                f"{MET_BASE_URL}/search",
                params={
                    "departmentId": department_id,
                    "hasImages": "true",
                    "q": "*",
                },
            )
            page_ids = [int(i) for i in (data.get("objectIDs") or [])]
            object_ids.extend(page_ids)
            total += int(data.get("total", len(page_ids)))

        return object_ids, total

    async def _painting_department_ids(self) -> list[str]:
        """Return painting/print department IDs, resolved from the API once.

        Department IDs are never hardcoded: they are read from /departments,
        ranked by how likely they are to carry movement metadata, and cached for
        the lifetime of the provider. Resolution failures degrade to the
        highlight feed rather than failing discovery.
        """
        if self._painting_departments is None:
            try:
                data = await self._client.get_json(f"{MET_BASE_URL}/departments")
            except Exception as e:
                logger.warning("met_departments_unavailable", error=str(e))
                self._painting_departments = []
                return []

            scored: list[tuple[int, int, str]] = []
            for department in data.get("departments", []):
                if not isinstance(department, dict):
                    continue
                department_id = department.get("departmentId")
                if department_id is None:
                    continue
                name = str(department.get("displayName", "")).lower()
                tier = _department_tier(name)
                if tier is None:
                    continue
                scored.append((tier, int(str(department_id)), str(department_id)))

            # Best tier first, then ascending id for reproducibility.
            scored.sort()
            self._painting_departments = [department_id for _, _, department_id in scored]
            logger.info(
                "met_painting_departments_resolved",
                count=len(self._painting_departments),
                departments=self._painting_departments,
            )

        return self._painting_departments

    async def get_artwork(self, object_id: str) -> Artwork | None:
        """Fetch a single Met object and translate to canonical Artwork."""
        resp = await self._client.get(f"{MET_BASE_URL}/objects/{object_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, dict) or data.get("objectID") is None:
            # A 200 response that is not an object means the contract changed or
            # the ID is wrong. Surface it instead of raising a bare KeyError.
            raise MuseumApiError(
                f"Met object {object_id} returned a payload without objectID "
                f"(keys: {sorted(data)[:8] if isinstance(data, dict) else type(data).__name__})."
            )
        return self._translate(data)

    async def search(self, query: SearchQuery) -> DiscoveryPage:
        """Search the Met collection."""
        params: dict[str, str] = {}
        if query.query:
            params["q"] = query.query
        else:
            params["q"] = "*"
        if query.public_domain_only:
            params["isPublicDomain"] = "true"
        if query.has_images:
            params["hasImages"] = "true"
        if query.department:
            params["departmentId"] = query.department

        resp = await self._client.get(f"{MET_BASE_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        object_ids: list[int] = data.get("objectIDs") or []
        total = data.get("total", len(object_ids))

        start = int(query.offset or 0)
        end = start + query.page_size
        page_ids = object_ids[start:end]

        artworks: list[Artwork] = []
        for obj_id in page_ids:
            artwork = await self._fetch_candidate(str(obj_id))
            if artwork is not None:
                artworks.append(artwork)

        return DiscoveryPage(
            artworks=artworks,
            total_count=total,
            has_more=end < len(object_ids),
            next_offset=end if end < len(object_ids) else None,
        )

    async def _fetch_candidate(self, object_id: str) -> Artwork | None:
        """Fetch one object for a bulk discovery pass.

        A single unusable record must not abort a whole run, so translation
        errors are logged and skipped. Single-artifact lookups still raise.
        """
        try:
            return await self.get_artwork(object_id)
        except MuseumApiError as e:
            logger.warning("met_object_unusable", object_id=object_id, error=str(e))
            return None

    def _translate(self, data: dict) -> Artwork:  # type: ignore[type-arg]
        """Translate a Met API object response into a canonical Artwork."""
        primary_image_url = data.get("primaryImage", "")
        primary_image = None
        if primary_image_url:
            primary_image = ImageInfo(url=primary_image_url)

        additional = []
        for url in data.get("additionalImages") or []:
            if url:
                additional.append(ImageInfo(url=url, is_primary=False))

        # Extract tags as flat list
        tags: list[str] = []
        for tag in data.get("tags") or []:
            if isinstance(tag, dict) and tag.get("term"):
                tags.append(tag["term"])
            elif isinstance(tag, str):
                tags.append(tag)

        return Artwork(
            identity=ArtworkIdentity(
                source="met",
                source_object_id=str(data["objectID"]),
            ),
            title=data.get("title", "Untitled"),
            artist_name=data.get("artistDisplayName") or None,
            artist_id=str(data["artistULAN_URL"]).split("/")[-1]
            if data.get("artistULAN_URL")
            else None,
            creation_date_text=data.get("objectDate") or None,
            year_start=_safe_int(data.get("objectBeginDate")),
            year_end=_safe_int(data.get("objectEndDate")),
            culture=data.get("culture") or None,
            department=data.get("department") or None,
            classification=data.get("classification") or None,
            object_type=data.get("objectName") or None,
            medium=data.get("medium") or None,
            movement_raw=None,  # Met doesn't have an explicit movement field
            period_raw=data.get("period") or None,
            style_raw=None,  # Will be derived from other fields
            tags=tags,
            description=None,  # Met API doesn't provide descriptions directly
            museum_name="The Metropolitan Museum of Art",
            museum_object_url=data.get("objectURL") or None,
            primary_image=primary_image,
            additional_images=additional,
            thumbnail_url=data.get("primaryImageSmall") or None,
            public_domain=data.get("isPublicDomain"),
            open_access=data.get("isPublicDomain"),  # Met equates these
            rights_text=data.get("rightsAndReproduction") or None,
            license=None,
            credit_line=data.get("creditLine") or None,
            metadata_raw=data,
        )


def _is_publishable_candidate(artwork: Artwork) -> bool:
    """Whether an object is worth sending through the publication pipeline.

    Requires an image and evidence that could support movement classification.
    Objects the museum marks as not public domain are excluded here; unknown
    rights are left for the rights gate to decide.
    """
    if artwork.primary_image is None:
        return False
    if artwork.public_domain is False:
        return False
    return _has_classification_signal(artwork)


def _has_classification_signal(artwork: Artwork) -> bool:
    """Whether the record carries any movement-classification evidence."""
    if artwork.period_raw or artwork.style_raw or artwork.movement_raw:
        return True
    if artwork.tags:
        return True

    haystack = " ".join(
        filter(None, (artwork.classification, artwork.object_type, artwork.department))
    ).lower()
    return any(keyword in haystack for keyword in _ART_CLASSIFICATION_KEYWORDS)


_ART_CLASSIFICATION_KEYWORDS = (
    "painting",
    "print",
    "drawing",
    "sketch",
    "photograph",
    "sculpture",
    "textile",
    "graphic",
    "manuscript",
    "book",
    "folio",
    "lithograph",
    "etching",
    "engraving",
    "watercolor",
    "tapestry",
    "relief",
    "coin",
    "medal",
)

# Department name tiers: lower is better. Objects in these departments usually
# carry movement metadata (period, style, or tags).
_DEPARTMENT_TIERS: tuple[tuple[str, ...], ...] = (
    ("painting",),
    ("modern art", "contemporary"),
    ("drawings and prints", "photographs"),
    ("medieval art", "ancient", "greek and roman", "egyptian", "islamic", "africa"),
)


def _department_tier(name: str) -> int | None:
    """Return the tier for a department display name, or None if it is excluded."""
    for tier, keywords in enumerate(_DEPARTMENT_TIERS):
        if any(keyword in name for keyword in keywords):
            return tier
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
