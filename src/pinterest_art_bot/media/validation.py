"""Media validation layer.

Validates image metadata, streams and validates image bytes, computes exact
SHA-256 (binary identity) and perceptual hashes (near-duplicate detection), and
enforces minimum dimension/quality constraints.

Downloads are streamed with a hard byte ceiling so a hostile or misconfigured
upstream can never force an unbounded allocation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from PIL import Image

from pinterest_art_bot.media.hashing import compute_perceptual_hash, compute_sha256

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    }
)

STREAM_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)


class MediaSettingsSource(Protocol):
    """Structural view of the media-related application settings.

    Keeps this module free of configuration imports while staying type safe.
    """

    min_image_width: int
    min_image_height: int
    max_image_file_size_mb: int
    perceptual_hash_enabled: bool


@dataclass(frozen=True)
class MediaRequirements:
    """Configurable media quality thresholds."""

    min_width: int = 600
    min_height: int = 600
    max_file_size_bytes: int = 20 * 1024 * 1024  # 20 MB
    allowed_content_types: frozenset[str] = ALLOWED_CONTENT_TYPES
    compute_perceptual_hash: bool = True

    @classmethod
    def from_settings(cls, settings: MediaSettingsSource) -> MediaRequirements:
        """Build requirements from application settings."""
        return cls(
            min_width=settings.min_image_width,
            min_height=settings.min_image_height,
            max_file_size_bytes=settings.max_image_file_size_mb * 1024 * 1024,
            compute_perceptual_hash=settings.perceptual_hash_enabled,
        )


@dataclass
class MediaValidationResult:
    """Result of media metadata validation."""

    is_valid: bool = True
    issues: list[str] = field(default_factory=list)

    def add_issue(self, issue: str) -> None:
        self.issues.append(issue)
        self.is_valid = False


@dataclass
class DownloadedMediaResult:
    """Result of image download and byte-level validation."""

    is_valid: bool = True
    sha256: str | None = None
    perceptual_hash: str | None = None
    width: int | None = None
    height: int | None = None
    byte_size: int = 0
    content_type: str | None = None
    # Raw bytes, retained only when the caller needs them for preparation
    # (base64 uploads). Skipped by default to avoid holding large images.
    data: bytes | None = None
    issues: list[str] = field(default_factory=list)

    def add_issue(self, issue: str) -> None:
        self.issues.append(issue)
        self.is_valid = False


class MediaValidator:
    """Validates image metadata and downloaded media against quality requirements."""

    def __init__(self, requirements: MediaRequirements | None = None) -> None:
        self._req = requirements or MediaRequirements()

    @property
    def requirements(self) -> MediaRequirements:
        return self._req

    def validate_metadata(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
        file_size_bytes: int | None = None,
        content_type: str | None = None,
        url: str | None = None,
    ) -> MediaValidationResult:
        """Validate image metadata against requirements."""
        result = MediaValidationResult()

        if not url:
            result.add_issue("No image URL available.")
            return result

        if width is not None and width < self._req.min_width:
            result.add_issue(f"Image width {width}px is below minimum {self._req.min_width}px.")

        if height is not None and height < self._req.min_height:
            result.add_issue(f"Image height {height}px is below minimum {self._req.min_height}px.")

        if file_size_bytes is not None and file_size_bytes > self._req.max_file_size_bytes:
            max_mb = self._req.max_file_size_bytes / (1024 * 1024)
            actual_mb = file_size_bytes / (1024 * 1024)
            result.add_issue(f"Image file size {actual_mb:.1f}MB exceeds maximum {max_mb:.1f}MB.")

        if content_type is not None and content_type not in self._req.allowed_content_types:
            result.add_issue(
                f"Content type '{content_type}' is not allowed. "
                f"Allowed: {', '.join(sorted(self._req.allowed_content_types))}."
            )

        return result

    async def download_and_validate(
        self,
        url: str | None,
        client: httpx.AsyncClient | None = None,
        *,
        keep_bytes: bool = False,
    ) -> DownloadedMediaResult:
        """Stream, verify, and fingerprint an image.

        Enforces the byte ceiling while streaming, then decodes dimensions and
        computes both exact and perceptual fingerprints.
        """
        result = DownloadedMediaResult()

        if not url:
            result.add_issue("No image URL available.")
            return result

        owns_client = client is None
        http = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "pinterest-artfolio-bot/0.1.0"},
        )

        try:
            try:
                async with http.stream("GET", url) as response:
                    if response.status_code != 200:
                        result.add_issue(
                            f"HTTP response failed with status {response.status_code} "
                            "for image URL."
                        )
                        return result

                    content_type = (
                        response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    )
                    result.content_type = content_type

                    if content_type and not (
                        content_type.startswith("image/")
                        or content_type in self._req.allowed_content_types
                    ):
                        result.add_issue(f"Unexpected Content-Type for image: {content_type}")
                        return result

                    declared_length = response.headers.get("Content-Length")
                    if declared_length and int(declared_length) > self._req.max_file_size_bytes:
                        max_mb = self._req.max_file_size_bytes / (1024 * 1024)
                        result.add_issue(
                            f"Image size {int(declared_length) / (1024 * 1024):.1f}MB "
                            f"exceeds limit of {max_mb:.1f}MB."
                        )
                        return result

                    content = await self._read_bounded(response)
            except httpx.HTTPError as e:
                result.add_issue(f"HTTP request failed for image URL: {e}")
                return result

            if content is None:
                max_mb = self._req.max_file_size_bytes / (1024 * 1024)
                result.add_issue(
                    f"Image download exceeded limit of {max_mb:.1f}MB while streaming."
                )
                return result

            byte_size = len(content)
            result.byte_size = byte_size

            if byte_size == 0:
                result.add_issue("Image response body is empty (0 bytes).")
                return result

            if byte_size > self._req.max_file_size_bytes:
                max_mb = self._req.max_file_size_bytes / (1024 * 1024)
                actual_mb = byte_size / (1024 * 1024)
                result.add_issue(f"Image size {actual_mb:.1f}MB exceeds limit of {max_mb:.1f}MB.")
                return result

            if keep_bytes:
                result.data = content

            result.sha256 = compute_sha256(content)

            if self._req.compute_perceptual_hash:
                result.perceptual_hash = compute_perceptual_hash(content)

            self._apply_dimensions(content, result)
            return result

        finally:
            if owns_client:
                await http.aclose()

    async def _read_bounded(self, response: httpx.Response) -> bytes | None:
        """Accumulate the body in chunks, aborting as soon as the cap is exceeded.

        Returns None when the stream exceeded the configured byte ceiling.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(STREAM_CHUNK_BYTES):
            total += len(chunk)
            if total > self._req.max_file_size_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _apply_dimensions(self, content: bytes, result: DownloadedMediaResult) -> None:
        """Decode dimensions and enforce the minimum resolution."""
        try:
            with Image.open(io.BytesIO(content)) as img:
                width, height = img.size
                result.width = width
                result.height = height
        except Exception as e:
            result.add_issue(f"Pillow failed to decode image: {e}")
            return

        if result.width is not None and result.width < self._req.min_width:
            result.add_issue(
                f"Image width {result.width}px is below minimum {self._req.min_width}px."
            )
        if result.height is not None and result.height < self._req.min_height:
            result.add_issue(
                f"Image height {result.height}px is below minimum {self._req.min_height}px."
            )
