"""Coverage for ARTIC adapter search/discovery paths and logging setup."""

import httpx

from pinterest_art_bot.museums.artic.adapter import ARTIC_BASE_URL, ArticMuseumProvider
from pinterest_art_bot.museums.base import MuseumProvider, SearchQuery

SEARCH_PAYLOAD = {
    "pagination": {"total": 5, "limit": 2, "offset": 0, "current_page": 1, "total_pages": 3},
    "data": [
        {
            "id": 16568,
            "title": "Water Lilies",
            "artist_display": "Claude Monet",
            "artist_id": 1,
            "date_display": "1906",
            "date_start": 1906,
            "date_end": 1906,
            "department_title": "Painting and Sculpture of Europe",
            "classification_titles": ["paintings"],
            "artwork_type_title": "Painting",
            "medium_display": "Oil on canvas",
            "style_title": "Impressionism",
            "style_titles": ["Impressionism"],
            "category_titles": ["paintings"],
            "description": "A pond of lilies.",
            "image_id": "abc123",
            "thumbnail": {"width": 843, "height": 843},
            "is_public_domain": True,
            "license_text": "CC0",
            "credit_line": "Gift of Mr. and Mrs. Lewis Larned Coburn",
        }
    ],
    "config": {"iiif_url": "https://www.artic.edu/iiif/2"},
}


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestArticAdapter:
    def test_identity(self):
        provider = ArticMuseumProvider()
        assert provider.source_id == "artic"
        assert provider.museum_name == "Art Institute of Chicago"
        assert isinstance(provider, MuseumProvider)

    async def test_discover_uses_iiif_and_pagination(self):
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(dict(request.url.params))
            return httpx.Response(200, json=SEARCH_PAYLOAD)

        async with transport(handler) as http:
            page = await ArticMuseumProvider(http).discover(page_size=2, offset=4)

        assert page.total_count == 5
        assert page.has_more is True
        # Resumes from the actual page position (offset 4 + 1 returned item),
        # not from the first page.
        assert page.next_offset == 5
        art = page.artworks[0]
        assert art.canonical_id == "artic:16568"
        assert art.style_raw == "Impressionism"
        assert art.license == "CC0"
        assert art.primary_image is not None
        assert "iiif/2/abc123" in art.primary_image.url
        assert art.thumbnail_url is not None
        assert calls[0]["page"] == "3"  # offset 4 / size 2 -> third page
        assert calls[0]["query[term][is_public_domain]"] == "true"

    async def test_search_applies_filters(self):
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(dict(request.url.params))
            return httpx.Response(200, json=SEARCH_PAYLOAD)

        async with transport(handler) as http:
            page = await ArticMuseumProvider(http).search(
                SearchQuery(query="monet", public_domain_only=True, has_images=True, page_size=2)
            )

        assert page.artworks[0].canonical_id == "artic:16568"
        assert calls[0]["q"] == "monet"
        assert calls[0]["limit"] == "2"

    async def test_items_without_image_are_skipped(self):
        payload = {
            **SEARCH_PAYLOAD,
            "data": [{**SEARCH_PAYLOAD["data"][0], "image_id": None}],
        }
        async with transport(lambda request: httpx.Response(200, json=payload)) as http:
            page = await ArticMuseumProvider(http).discover()
        assert page.artworks == []

    async def test_get_artwork_returns_none_on_404(self):
        async with transport(lambda request: httpx.Response(404)) as http:
            assert await ArticMuseumProvider(http).get_artwork("1") is None

    async def test_final_page_reports_no_more(self):
        payload = {
            "pagination": {
                "total": 1,
                "limit": 2,
                "offset": 0,
                "current_page": 1,
                "total_pages": 1,
            },
            "data": SEARCH_PAYLOAD["data"],
            "config": SEARCH_PAYLOAD["config"],
        }
        async with transport(lambda request: httpx.Response(200, json=payload)) as http:
            page = await ArticMuseumProvider(http).discover()
        assert page.has_more is False
        assert page.next_offset is None

    def test_base_url_is_https(self):
        assert ARTIC_BASE_URL.startswith("https://")


class TestLoggingSetup:
    def test_json_format_configures_stdlib_handler(self):
        import logging

        from pinterest_art_bot.logging.setup import setup_logging

        setup_logging(level="DEBUG", fmt="json")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert root.handlers, "a log handler must be installed"
        # stdout must stay clean for command output, so logs go to stderr.
        import sys

        assert root.handlers[0].stream is sys.stderr

    def test_console_format_and_invalid_level_fallback(self):
        import logging

        from pinterest_art_bot.logging.setup import setup_logging

        setup_logging(level="not-a-level", fmt="console")
        assert logging.getLogger().level == logging.INFO

    def test_structured_log_emits_key_values(self, capsys):
        import structlog

        from pinterest_art_bot.logging.setup import setup_logging

        setup_logging(level="INFO", fmt="json")
        structlog.get_logger("test").info("event_happened", artwork_id="met:1")
        captured = capsys.readouterr()
        assert "event_happened" in captured.err
        assert '"artwork_id": "met:1"' in captured.err
        assert captured.out == ""
