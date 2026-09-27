import pytest

from pinterest_art_bot.media.validation import MediaRequirements, MediaValidator


class TestMediaValidator:
    """Media validation must enforce quality thresholds."""

    def setup_method(self):
        self.validator = MediaValidator()

    def test_valid_image(self):
        result = self.validator.validate_metadata(
            width=2000, height=1500, url="https://example.com/image.jpg"
        )
        assert result.is_valid is True
        assert result.issues == []

    def test_no_url_is_invalid(self):
        result = self.validator.validate_metadata(width=2000, height=1500)
        assert result.is_valid is False
        assert any("URL" in issue for issue in result.issues)

    def test_too_small_width(self):
        result = self.validator.validate_metadata(
            width=400, height=1500, url="https://example.com/image.jpg"
        )
        assert result.is_valid is False
        assert any("width" in issue.lower() for issue in result.issues)

    def test_too_small_height(self):
        result = self.validator.validate_metadata(
            width=2000, height=300, url="https://example.com/image.jpg"
        )
        assert result.is_valid is False
        assert any("height" in issue.lower() for issue in result.issues)

    def test_file_too_large(self):
        result = self.validator.validate_metadata(
            file_size_bytes=50 * 1024 * 1024,
            url="https://example.com/image.jpg",
        )
        assert result.is_valid is False
        assert any("size" in issue.lower() for issue in result.issues)

    def test_invalid_content_type(self):
        result = self.validator.validate_metadata(
            content_type="application/pdf",
            url="https://example.com/doc.pdf",
        )
        assert result.is_valid is False

    def test_valid_content_types(self):
        for ct in ["image/jpeg", "image/png", "image/webp"]:
            result = self.validator.validate_metadata(
                content_type=ct, url="https://example.com/img"
            )
            assert ct not in [issue for issue in result.issues]

    def test_custom_requirements(self):
        validator = MediaValidator(requirements=MediaRequirements(min_width=1000, min_height=1000))
        result = validator.validate_metadata(
            width=800, height=800, url="https://example.com/image.jpg"
        )
        assert result.is_valid is False

    def test_unknown_dimensions_pass(self):
        """If dimensions are not known, don't reject — we can't validate yet."""
        result = self.validator.validate_metadata(url="https://example.com/image.jpg")
        assert result.is_valid is True


class TestDownloadAndValidate:
    """Byte-level image validation and exact SHA-256 calculation."""

    @pytest.fixture
    def sample_jpeg_bytes(self) -> bytes:
        import io

        from PIL import Image

        img = Image.new("RGB", (800, 800), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_valid_image_download(self, sample_jpeg_bytes):
        import hashlib

        import httpx

        def handler(request):
            return httpx.Response(
                200,
                headers={"Content-Type": "image/jpeg"},
                content=sample_jpeg_bytes,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            validator = MediaValidator()
            res = await validator.download_and_validate(
                "https://example.com/art.jpg", client=client
            )

        assert res.is_valid is True
        assert res.sha256 == hashlib.sha256(sample_jpeg_bytes).hexdigest()
        assert res.width == 800
        assert res.height == 800
        assert res.byte_size == len(sample_jpeg_bytes)
        assert res.issues == []

    @pytest.mark.asyncio
    async def test_image_dimensions_below_minimum(self):
        import io

        import httpx
        from PIL import Image

        # 300x300 is below 600x600 minimum
        img = Image.new("RGB", (300, 300), color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        bytes_data = buf.getvalue()

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"Content-Type": "image/png"}, content=bytes_data
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            validator = MediaValidator()
            res = await validator.download_and_validate(
                "https://example.com/small.png", client=client
            )

        assert res.is_valid is False
        assert any("width" in issue.lower() for issue in res.issues)

    @pytest.mark.asyncio
    async def test_empty_image_response(self):
        import httpx

        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            validator = MediaValidator()
            res = await validator.download_and_validate(
                "https://example.com/empty.jpg", client=client
            )

        assert res.is_valid is False
        assert any("empty" in issue.lower() for issue in res.issues)

    @pytest.mark.asyncio
    async def test_http_error_response(self):
        import httpx

        transport = httpx.MockTransport(lambda req: httpx.Response(404, content=b"Not Found"))
        async with httpx.AsyncClient(transport=transport) as client:
            validator = MediaValidator()
            res = await validator.download_and_validate(
                "https://example.com/missing.jpg", client=client
            )

        assert res.is_valid is False
        assert any("404" in issue for issue in res.issues)
