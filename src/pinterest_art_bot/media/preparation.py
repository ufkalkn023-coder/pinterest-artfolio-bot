"""Image preparation for publishing.

Museum images are archival: enormous, arbitrarily proportioned TIFFs and JPEGs
of paintings that are usually far from Pinterest's 2:3 portrait preference.
Publishing them raw wastes bandwidth and looks worse in the feed, so this module
produces a Pin-ready derivative.

Two media modes are supported, because hotlinking and uploading have different
failure modes:

  - ``url``     pass the museum URL to Pinterest. Zero infrastructure, but the
                Pin breaks if the museum's CDN later blocks or removes it.
  - ``base64``  upload the prepared image with the Pin. Self-contained, at the
                cost of a larger request body.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from enum import StrEnum

import structlog
from PIL import Image, ImageOps

from pinterest_art_bot.media.hashing import compute_perceptual_hash, compute_sha256

logger = structlog.get_logger()

# Pinterest's preferred Pin aspect ratio.
PREFERRED_ASPECT_RATIO = 2 / 3
ASPECT_TOLERANCE = 0.12
DEFAULT_MAX_WIDTH = 1000
DEFAULT_JPEG_QUALITY = 88
MAX_SIDE_BUDGET = 4096


class MediaMode(StrEnum):
    """How the image reaches Pinterest."""

    URL = "url"
    BASE64 = "base64"


@dataclass(frozen=True)
class PreparedImage:
    """A Pin-ready image plus the fingerprints taken from the original."""

    data: bytes
    width: int
    height: int
    content_type: str = "image/jpeg"
    transformed: bool = False
    original_sha256: str | None = None
    sha256: str | None = None
    perceptual_hash: str | None = None

    @property
    def aspect_ratio(self) -> float:
        """Width divided by height."""
        return self.width / self.height if self.height else 0.0

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def to_base64(self) -> str:
        """Encode for Pinterest's `media_base64` field."""
        return base64.b64encode(self.data).decode("ascii")


def aspect_ratio_is_preferred(width: int, height: int) -> bool:
    """Whether an image is close enough to Pinterest's 2:3 preference."""
    if not width or not height:
        return False
    ratio = width / height
    return abs(ratio - PREFERRED_ASPECT_RATIO) <= ASPECT_TOLERANCE


class ImagePreparer:
    """Turns raw museum bytes into a Pin-ready image."""

    def __init__(
        self,
        *,
        max_width: int = DEFAULT_MAX_WIDTH,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        mode: MediaMode = MediaMode.URL,
    ) -> None:
        self._max_width = max_width
        self._quality = jpeg_quality
        self._mode = mode

    @property
    def mode(self) -> MediaMode:
        return self._mode

    def prepare(
        self,
        data: bytes,
        *,
        source_url: str | None = None,
        compute_fingerprints: bool = True,
    ) -> PreparedImage | None:
        """Prepare an image, returning None when it cannot be decoded.

        Fingerprints are always taken from the ORIGINAL bytes, so deduplication
        keeps working even though the published bytes differ.
        """
        original_sha = compute_sha256(data) if compute_fingerprints else None
        perceptual = compute_perceptual_hash(data) if compute_fingerprints else None

        try:
            with Image.open(io.BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image)
                needs_conversion = image.mode not in ("RGB", "L")
                working = image.convert("RGB") if needs_conversion else image.copy()

                transformed = False

                if working.width > MAX_SIDE_BUDGET or working.height > MAX_SIDE_BUDGET:
                    working.thumbnail((MAX_SIDE_BUDGET, MAX_SIDE_BUDGET))
                    transformed = True

                if working.width > self._max_width:
                    ratio = self._max_width / working.width
                    working = working.resize(
                        (self._max_width, max(1, int(working.height * ratio))),
                        Image.Resampling.LANCZOS,
                    )
                    transformed = True

                output = io.BytesIO()
                working.save(output, format="JPEG", quality=self._quality, optimize=True)
                payload = output.getvalue()
                final_width, final_height = working.size
        except Exception as e:
            # An undecodable image is not fatal: the caller falls back to the URL.
            logger.warning("image_prepare_failed", error=str(e), source_url=source_url)
            return None

        return PreparedImage(
            data=payload,
            width=final_width,
            height=final_height,
            transformed=transformed,
            original_sha256=original_sha,
            sha256=compute_sha256(payload) if compute_fingerprints else None,
            perceptual_hash=perceptual,
        )
