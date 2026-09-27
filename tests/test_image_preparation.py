"""Tests for image preparation and the base64 media mode."""

import base64
import io

import pytest
from PIL import Image

from pinterest_art_bot.media.preparation import (
    PREFERRED_ASPECT_RATIO,
    ImagePreparer,
    MediaMode,
    aspect_ratio_is_preferred,
)


def make_image(width: int = 800, height: int = 1200, seed: int = 0) -> bytes:
    """A structured image, so perceptual hashes are meaningful."""
    img = Image.new("RGB", (width, height), (20 + seed, 90, 160))
    for i in range(0, width, 40):
        for j in range(0, height, 40):
            if (i + j) % 80 == 0:
                img.putpixel((i, j), (250, 240, 10))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_tiff(width: int = 4000, height: int = 6000) -> bytes:
    """A large archival-style image."""
    img = Image.new("RGB", (width, height), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="TIFF", compression="tiff_lzw")
    return buf.getvalue()


class TestAspectRatio:
    def test_preferred_ratio(self):
        assert aspect_ratio_is_preferred(1000, 1500) is True
        assert round(1000 / 1500, 3) == round(PREFERRED_ASPECT_RATIO, 3)

    def test_wildly_off_ratio(self):
        assert aspect_ratio_is_preferred(3000, 500) is False

    def test_zero_dimensions(self):
        assert aspect_ratio_is_preferred(0, 100) is False
        assert aspect_ratio_is_preferred(100, 0) is False

    def test_prepared_image_reports_ratio(self):
        prepared = ImagePreparer().prepare(make_image(1000, 1500))
        assert prepared is not None
        assert prepared.aspect_ratio == pytest.approx(2 / 3, abs=0.01)


class TestPreparation:
    def test_oversized_image_is_downscaled(self):
        prepared = ImagePreparer(max_width=1000).prepare(make_image(2400, 3600))
        assert prepared is not None
        assert prepared.width == 1000
        assert prepared.transformed is True
        assert prepared.size_bytes < 2400 * 3600

    def test_huge_tiff_is_bounded(self):
        prepared = ImagePreparer(max_width=1000).prepare(make_tiff())
        assert prepared is not None
        assert prepared.width <= 1000
        assert max(prepared.width, prepared.height) <= 4096

    def test_small_image_is_not_enlarged(self):
        prepared = ImagePreparer(max_width=2000).prepare(make_image(400, 600))
        assert prepared is not None
        assert prepared.width == 400
        assert prepared.transformed is False

    def test_output_is_a_valid_jpeg(self):
        prepared = ImagePreparer().prepare(make_image())
        assert prepared is not None
        with Image.open(io.BytesIO(prepared.data)) as img:
            assert img.format == "JPEG"
            assert img.mode == "RGB"

    def test_transparent_input_is_flattened(self):
        img = Image.new("RGBA", (800, 1200), (255, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        prepared = ImagePreparer().prepare(buf.getvalue())
        assert prepared is not None
        with Image.open(io.BytesIO(prepared.data)) as out:
            assert out.mode == "RGB"

    def test_undecodable_bytes_return_none(self):
        assert ImagePreparer().prepare(b"not an image") is None

    def test_fingerprints_come_from_the_original(self):
        """Deduplication must survive re-encoding."""
        original = make_image(2400, 3600, seed=3)
        prepared = ImagePreparer(max_width=800).prepare(original)
        assert prepared is not None
        assert prepared.original_sha256 != prepared.sha256, "output differs from input"
        assert prepared.original_sha256 is not None
        assert prepared.perceptual_hash is not None

    def test_fingerprinting_can_be_skipped(self):
        prepared = ImagePreparer().prepare(make_image(), compute_fingerprints=False)
        assert prepared is not None
        assert prepared.original_sha256 is None
        assert prepared.perceptual_hash is None

    def test_repeated_preparation_is_deterministic(self):
        image = make_image(1600, 2400)
        first = ImagePreparer(max_width=900).prepare(image)
        second = ImagePreparer(max_width=900).prepare(image)
        assert first is not None and second is not None
        assert first.data == second.data

    def test_base64_encoding(self):
        prepared = ImagePreparer().prepare(make_image())
        assert prepared is not None
        encoded = prepared.to_base64()
        assert base64.b64decode(encoded) == prepared.data

    def test_quality_setting_changes_size(self):
        image = make_image(1600, 2400)
        low = ImagePreparer(jpeg_quality=40).prepare(image)
        high = ImagePreparer(jpeg_quality=95).prepare(image)
        assert low is not None and high is not None
        assert low.size_bytes < high.size_bytes


class TestMediaMode:
    def test_modes_are_explicit(self):
        assert MediaMode("url") == MediaMode.URL
        assert MediaMode("base64") == MediaMode.BASE64
        with pytest.raises(ValueError):
            MediaMode("ftp")
