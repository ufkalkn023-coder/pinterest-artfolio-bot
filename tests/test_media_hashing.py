"""Tests for perceptual hashing and media download safeguards."""

import httpx
from PIL import Image, ImageDraw

from pinterest_art_bot.media.hashing import (
    compute_perceptual_hash,
    compute_sha256,
    hamming_distance,
    is_degenerate,
    is_near_duplicate,
)
from pinterest_art_bot.media.validation import MediaRequirements, MediaValidator


def patterned_image(seed: int = 0, size: int = 800) -> Image.Image:
    """A structured image (not flat colour, so hashes are informative)."""
    img = Image.new("RGB", (size, size), color=(30 + seed, 80, 140))
    draw = ImageDraw.Draw(img)
    for i in range(-size, size * 2, 40 + seed):
        draw.line([(i, 0), (i + size, size)], fill=(240, 200, 30), width=11)
    return img


def vertical_image(size: int = 800) -> Image.Image:
    """A structurally different image (vertical bands, no diagonals)."""
    img = Image.new("RGB", (size, size), color=(200, 200, 10))
    draw = ImageDraw.Draw(img)
    for x in range(0, size, 60):
        draw.rectangle([x, 0, x + 30, size], fill=(10, 10, 220))
    return img


def encode(img: Image.Image, quality: int = 90) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class TestPerceptualHash:
    def test_hash_is_stable_across_recompression(self):
        img = patterned_image()
        high = compute_perceptual_hash(encode(img, 95))
        low = compute_perceptual_hash(encode(img, 30))
        assert high is not None and low is not None
        assert hamming_distance(high, low) <= 4

    def test_hash_is_stable_across_resize(self):
        img = patterned_image()
        full = compute_perceptual_hash(encode(img, 90))
        small = compute_perceptual_hash(encode(img.resize((160, 160)), 90))
        assert full is not None and small is not None
        assert hamming_distance(full, small) <= 12

    def test_different_images_differ(self):
        first = compute_perceptual_hash(encode(patterned_image(0)))
        second = compute_perceptual_hash(encode(vertical_image()))
        assert first is not None and second is not None
        assert hamming_distance(first, second) > 12

    def test_hash_is_hex_encoded(self):
        digest = compute_perceptual_hash(encode(patterned_image()))
        assert digest is not None
        assert len(digest) == 16
        int(digest, 16)

    def test_undecodable_bytes_return_none(self):
        assert compute_perceptual_hash(b"not an image") is None

    def test_flat_image_is_degenerate(self):
        flat = Image.new("RGB", (800, 800), color=(12, 34, 56))
        assert is_degenerate(compute_perceptual_hash(encode(flat))) is True

    def test_patterned_image_is_not_degenerate(self):
        assert is_degenerate(compute_perceptual_hash(encode(patterned_image()))) is False

    def test_empty_and_invalid_are_degenerate(self):
        assert is_degenerate(None) is True
        assert is_degenerate("") is True
        assert is_degenerate("zzzz") is True

    def test_incomparable_hashes_are_far_apart(self):
        assert hamming_distance("abcd", "abcd") == 0
        assert hamming_distance("abcd", "abce") == 2
        assert hamming_distance("abcd", "ab") > 64
        assert hamming_distance("abcd", "zzzz") > 64

    def test_is_near_duplicate_respects_threshold(self):
        assert is_near_duplicate("00ff", "00fe", max_distance=1) is True
        assert is_near_duplicate("00ff", "00fe", max_distance=0) is False
        assert is_near_duplicate("00ff", "00fe", max_distance=-1) is False

    def test_sha256_matches_hashlib(self):
        import hashlib

        assert compute_sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


class TestStreamingDownload:
    def requirements(self, **overrides) -> MediaRequirements:
        base = {"min_width": 100, "min_height": 100, "max_file_size_bytes": 2 * 1024 * 1024}
        base.update(overrides)
        return MediaRequirements(**base)  # type: ignore[arg-type]

    async def test_computes_both_fingerprints(self):
        payload = encode(patterned_image())
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=payload
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await MediaValidator(self.requirements()).download_and_validate(
                "https://example.com/a.jpg", client=client
            )

        assert result.is_valid is True
        assert result.sha256 == compute_sha256(payload)
        assert result.perceptual_hash is not None
        assert result.width == 800
        assert result.byte_size == len(payload)

    async def test_perceptual_hash_can_be_disabled(self):
        payload = encode(patterned_image())
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=payload
            )
        )
        validator = MediaValidator(self.requirements(compute_perceptual_hash=False))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await validator.download_and_validate(
                "https://example.com/a.jpg", client=client
            )
        assert result.perceptual_hash is None
        assert result.is_valid is True

    async def test_oversized_body_is_rejected_via_content_length(self):
        payload = encode(patterned_image()) * 40  # far above the ceiling
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=payload
            )
        )
        validator = MediaValidator(self.requirements(max_file_size_bytes=1024))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await validator.download_and_validate(
                "https://example.com/big.jpg", client=client
            )

        assert result.is_valid is False
        assert any("exceeds limit" in issue for issue in result.issues)

    async def test_stream_ceiling_aborts_when_length_is_unknown(self):
        """Chunked responses must still be capped while streaming."""

        async def body():
            chunk = encode(patterned_image(), quality=98)
            for _ in range(60):
                yield chunk

        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=body()
            )
        )
        validator = MediaValidator(self.requirements(max_file_size_bytes=64 * 1024))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await validator.download_and_validate(
                "https://example.com/stream.jpg", client=client
            )

        assert result.is_valid is False
        assert any("exceeded limit" in issue for issue in result.issues)
        assert result.byte_size == 0

    async def test_declared_content_length_is_rejected_early(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "image/jpeg", "Content-Length": "99999999"},
                content=encode(patterned_image()),
            )
        )
        validator = MediaValidator(self.requirements(max_file_size_bytes=1024))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await validator.download_and_validate(
                "https://example.com/x.jpg", client=client
            )

        assert result.is_valid is False
        assert any("exceeds limit" in issue for issue in result.issues)

    async def test_minimum_dimensions_enforced(self):
        small = encode(patterned_image(size=120))
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=small
            )
        )
        validator = MediaValidator(self.requirements(min_width=600, min_height=600))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await validator.download_and_validate(
                "https://example.com/s.jpg", client=client
            )

        assert result.is_valid is False
        assert any("below minimum" in issue for issue in result.issues)

    async def test_non_image_content_type_rejected(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Type": "text/html"}, content=b"<html>"
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await MediaValidator(self.requirements()).download_and_validate(
                "https://example.com/page", client=client
            )
        assert result.is_valid is False

    async def test_network_error_is_issue_not_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await MediaValidator(self.requirements()).download_and_validate(
                "https://example.com/a.jpg", client=client
            )
        assert result.is_valid is False
        assert any("HTTP request failed" in issue for issue in result.issues)


class TestRequirementsFromSettings:
    def test_built_from_settings(self):
        from pinterest_art_bot.config.settings import Settings

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            min_image_width=800,
            min_image_height=900,
            max_image_file_size_mb=5,
            perceptual_hash_enabled=False,
        )
        requirements = MediaRequirements.from_settings(settings)

        assert requirements.min_width == 800
        assert requirements.min_height == 900
        assert requirements.max_file_size_bytes == 5 * 1024 * 1024
        assert requirements.compute_perceptual_hash is False

    def test_validator_exposes_requirements(self):
        requirements = MediaRequirements(min_width=10)
        assert MediaValidator(requirements).requirements is requirements

    def test_missing_url_is_invalid(self):
        result = MediaValidator().validate_metadata(url=None)
        assert result.is_valid is False
