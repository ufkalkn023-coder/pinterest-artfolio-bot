"""Canonical artwork domain model.

Every museum adapter translates its own API representation into this
single internal format.  Fields are divided into:

  - Required identity fields (must always be present)
  - Core metadata (strongly encouraged, may be absent for some sources)
  - Optional enrichment (nice-to-have, often missing)
  - Raw / provenance (preserved for debugging and auditing)
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field


class ArtworkIdentity(BaseModel):
    """Stable identity for an artwork across the system.

    Primary key: (source, source_object_id)
    Never rely on title or artist alone — they are ambiguous and mutable.
    """

    source: str = Field(
        ...,
        description="Canonical museum/source identifier, e.g. 'met', 'artic', 'rijksmuseum'.",
    )
    source_object_id: str = Field(
        ...,
        description="Object ID within the source museum's system.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def canonical_id(self) -> str:
        """Deterministic global identity string, e.g. 'met:436535'."""
        return f"{self.source}:{self.source_object_id}"


class ImageInfo(BaseModel):
    """Information about an available image for the artwork."""

    url: str
    width: int | None = None
    height: int | None = None
    is_primary: bool = True

    # Populated after download
    sha256: str | None = Field(
        default=None,
        description="SHA-256 hex digest of the downloaded image bytes.",
    )
    perceptual_hash: str | None = Field(
        default=None,
        description="Perceptual hash for near-duplicate detection (future).",
    )
    content_type: str | None = None
    file_size_bytes: int | None = None


class Artwork(BaseModel):
    """Canonical normalized artwork record.

    This is THE single internal representation of an artwork.
    All museum adapters produce instances of this model.
    All downstream consumers (classification, routing, publishing) read from it.
    """

    # ── Identity (REQUIRED) ─────────────────────────────────────────
    identity: ArtworkIdentity

    # ── Core metadata ───────────────────────────────────────────────
    title: str
    artist_name: str | None = None
    artist_id: str | None = Field(
        default=None,
        description="Artist identifier within the source museum.",
    )

    # ── Dates ───────────────────────────────────────────────────────
    creation_date_text: str | None = Field(
        default=None,
        description="Free-text date as displayed by the museum, e.g. 'ca. 1503-1519'.",
    )
    year_start: int | None = None
    year_end: int | None = None

    # ── Cultural context ────────────────────────────────────────────
    culture: str | None = None
    department: str | None = None
    classification: str | None = Field(
        default=None,
        description="Museum's own classification, e.g. 'Paintings', 'Drawings'.",
    )
    object_type: str | None = None
    medium: str | None = None

    # ── Style / movement raw metadata (pre-normalization) ──────────
    movement_raw: str | None = None
    period_raw: str | None = None
    style_raw: str | None = None
    tags: list[str] = Field(default_factory=list)

    # ── Description ─────────────────────────────────────────────────
    description: str | None = None

    # ── Museum provenance ───────────────────────────────────────────
    museum_name: str
    museum_object_url: str | None = None

    # ── Images ──────────────────────────────────────────────────────
    primary_image: ImageInfo | None = None
    additional_images: list[ImageInfo] = Field(default_factory=list)
    thumbnail_url: str | None = None

    # ── Rights ──────────────────────────────────────────────────────
    public_domain: bool | None = None
    open_access: bool | None = None
    rights_text: str | None = None
    license: str | None = None
    credit_line: str | None = None

    # ── Raw provenance ──────────────────────────────────────────────
    metadata_raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Original unmodified API response, preserved for auditing.",
    )
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ── Convenience accessors ───────────────────────────────────────

    @property
    def canonical_id(self) -> str:
        return self.identity.canonical_id

    @property
    def image_url(self) -> str | None:
        if self.primary_image:
            return self.primary_image.url
        return None

    @property
    def image_width(self) -> int | None:
        if self.primary_image:
            return self.primary_image.width
        return None

    @property
    def image_height(self) -> int | None:
        if self.primary_image:
            return self.primary_image.height
        return None

    @property
    def best_image_url(self) -> str | None:
        """Return the highest-resolution available image URL."""
        if self.primary_image:
            return self.primary_image.url
        if self.additional_images:
            # Prefer largest by pixel area
            with_dims = [img for img in self.additional_images if img.width and img.height]
            if with_dims:
                return max(with_dims, key=lambda i: (i.width or 0) * (i.height or 0)).url
            return self.additional_images[0].url
        return self.thumbnail_url

    def compute_metadata_fingerprint(self) -> str:
        """Compute a deterministic hash of normalized metadata.

        Includes: normalized artist, title, source, and source object ID.
        This is a metadata fingerprint for detecting record changes or matches.
        This is NOT a binary image hash.
        """
        raw_components = [
            (self.artist_name or "").strip().lower(),
            self.title.strip().lower(),
            self.identity.source.strip().lower(),
            self.identity.source_object_id.strip().lower(),
        ]
        payload = "|".join(raw_components).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def compute_image_sha256(image_bytes: bytes) -> str:
        """Compute exact SHA-256 hex digest of downloaded image bytes.

        This represents the exact binary identity of the media file.
        """
        return hashlib.sha256(image_bytes).hexdigest()

    @staticmethod
    def compute_image_hash(image_bytes: bytes) -> str:
        """Alias for compute_image_sha256 for backwards compatibility."""
        return Artwork.compute_image_sha256(image_bytes)
