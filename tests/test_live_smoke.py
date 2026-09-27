"""Opt-in live network smoke tests against real museum APIs.

These tests execute real READ-only GET requests against official museum APIs.
They are marked with @pytest.mark.live and deselected by default in CI/standard pytest.
To run them: pytest -m live
"""

import pytest

from pinterest_art_bot.museums.artic.adapter import ArticMuseumProvider
from pinterest_art_bot.museums.cleveland.adapter import ClevelandMuseumProvider
from pinterest_art_bot.museums.met.adapter import MetMuseumProvider


@pytest.mark.live
class TestLiveMuseumReads:
    """Read-only smoke tests using live museum APIs."""

    @pytest.mark.asyncio
    async def test_live_artic_discover(self):
        provider = ArticMuseumProvider()
        page = await provider.discover(page_size=2)
        assert len(page.artworks) > 0
        art = page.artworks[0]
        assert art.identity.source == "artic"
        assert art.title is not None
        assert art.primary_image is not None
        assert "http" in art.primary_image.url

    @pytest.mark.asyncio
    async def test_live_met_discover(self):
        provider = MetMuseumProvider()
        page = await provider.discover(page_size=2)
        assert len(page.artworks) > 0
        art = page.artworks[0]
        assert art.identity.source == "met"
        assert art.title is not None
        assert art.primary_image is not None
        assert "http" in art.primary_image.url

    @pytest.mark.asyncio
    async def test_live_cleveland_discover(self):
        provider = ClevelandMuseumProvider()
        page = await provider.discover(page_size=2)
        assert len(page.artworks) > 0
        art = page.artworks[0]
        assert art.identity.source == "cleveland"
        assert art.title is not None
        assert art.primary_image is not None
        assert "http" in art.primary_image.url

    @pytest.mark.asyncio
    async def test_live_met_search(self):
        provider = MetMuseumProvider()
        from pinterest_art_bot.museums.base import SearchQuery

        page = await provider.search(SearchQuery(query="van gogh", page_size=2))
        assert page.artworks
        assert all(a.identity.source == "met" for a in page.artworks)

    @pytest.mark.asyncio
    async def test_live_media_download(self):
        """Download and validate one real museum image end to end."""
        from pinterest_art_bot.media.validation import MediaValidator

        page = await ClevelandMuseumProvider().discover(page_size=1)
        assert page.artworks
        url = page.artworks[0].best_image_url
        assert url is not None

        result = await MediaValidator().download_and_validate(url)
        assert result.is_valid, result.issues
        assert result.sha256
        assert result.perceptual_hash
