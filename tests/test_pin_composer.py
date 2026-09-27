"""Tests for Pin metadata composition, including licence disclosure."""

import pytest

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity, ImageInfo
from pinterest_art_bot.domain.publication import BoardTarget
from pinterest_art_bot.domain.rights import (
    RightsEvaluation,
    RightsEvidence,
    RightsEvidenceSource,
    RightsStatus,
)
from pinterest_art_bot.pipeline.pin_composer import (
    LICENSE_URLS,
    PinComposer,
    sanitize,
    truncate,
)

BOARD = BoardTarget(board_key="impressionism", board_name="Impressionism", board_id="55")


def artwork(**overrides) -> Artwork:
    data = {
        "identity": ArtworkIdentity(source="met", source_object_id="1"),
        "title": "Water Lilies",
        "artist_name": "Claude Monet",
        "creation_date_text": "1906",
        "medium": "Oil on canvas",
        "museum_name": "The Metropolitan Museum of Art",
        "museum_object_url": "https://www.metmuseum.org/art/1",
        "public_domain": True,
        "primary_image": ImageInfo(url="https://example.com/a.jpg", width=800, height=800),
    }
    data.update(overrides)
    return Artwork(**data)


def evaluation(status: RightsStatus, **overrides) -> RightsEvaluation:
    data = {
        "artwork_id": "met:1",
        "status": status,
        "is_publishable": status in (RightsStatus.PUBLIC_DOMAIN, RightsStatus.CC0),
        "requires_attribution": status in (RightsStatus.CC_BY, RightsStatus.CC_BY_SA),
        "evidence": [
            RightsEvidence(
                source=RightsEvidenceSource.MUSEUM_API_FLAG,
                interpretation=status,
            )
        ],
    }
    data.update(overrides)
    return RightsEvaluation(**data)


class TestTruncationAndSanitizing:
    def test_truncate_marks_truncation(self):
        assert truncate("abcdefghij", 100) == "abcdefghij"
        trimmed = truncate("abcdefghij", 5)
        assert len(trimmed) == 5
        assert trimmed.endswith("…")

    def test_truncate_handles_zero_limit(self):
        assert truncate("abc", 0) == "abc"

    def test_sanitize_strips_control_characters(self):
        assert "\x00" not in sanitize("title\x00here")
        assert "\r" not in sanitize("line\rhere")
        assert "\t" not in sanitize("tabbed\there")

    def test_sanitize_collapses_whitespace_and_drops_blank_lines(self):
        assert sanitize("a  b\n\n\nc   d") == "a b\nc d"


class TestTitleAndAltText:
    def test_title_includes_artist(self):
        assert PinComposer().build_title(artwork()) == "Water Lilies — Claude Monet"

    def test_title_handles_missing_artist(self):
        assert "Unknown Artist" in PinComposer().build_title(artwork(artist_name=None))

    def test_title_respects_configured_limit(self):
        composer = PinComposer(title_max_length=12)
        assert len(composer.build_title(artwork())) == 12

    def test_alt_text_describes_the_work(self):
        alt = PinComposer().build_alt_text(artwork())
        assert "Water Lilies" in alt
        assert "Claude Monet" in alt

    def test_alt_text_respects_limit(self):
        composer = PinComposer(alt_text_max_length=15)
        assert len(composer.build_alt_text(artwork())) == 15

    def test_limits_are_reported(self):
        assert PinComposer().limits == {"title": 100, "description": 800, "alt_text": 500}


class TestLicenseDisclosure:
    def test_public_domain_states_source_credit(self):
        description = PinComposer().build_description(
            artwork(credit_line="Purchase, 1993"),
            "impressionism",
            evaluation(RightsStatus.PUBLIC_DOMAIN),
        )
        assert "License: Public domain" in description
        assert "Credit: Purchase, 1993" in description

    def test_cc0_includes_canonical_url(self):
        description = PinComposer().build_description(
            artwork(), "impressionism", evaluation(RightsStatus.CC0)
        )
        assert LICENSE_URLS[RightsStatus.CC0] in description
        assert "CC0" in description

    @pytest.mark.parametrize(
        "status", [RightsStatus.CC_BY, RightsStatus.CC_BY_SA, RightsStatus.ATTRIBUTION_REQUIRED]
    )
    def test_attribution_licences_state_credit_and_url(self, status):
        evaluation_ = evaluation(
            status, requires_attribution=True, attribution_text="Photo by Museum"
        )
        description = PinComposer().build_description(
            artwork(credit_line="Photo by Museum"), "impressionism", evaluation_
        )
        assert "Credit: Photo by Museum" in description
        assert "License:" in description
        label = description.split("License: ")[1].splitlines()[0]
        assert status.value.replace("_", " ").title()[:8].title() in label or "CC BY" in label

    def test_attribution_without_credit_line_still_declares_licence(self):
        evaluation_ = evaluation(RightsStatus.CC_BY, requires_attribution=True)
        description = PinComposer().build_description(artwork(), "impressionism", evaluation_)
        assert "not stated by the source museum" in description

    def test_every_publishable_status_has_a_label(self):
        """Statuses that can reach publication must be described explicitly."""
        from pinterest_art_bot.domain.rights import PUBLISHABLE_STATUSES
        from pinterest_art_bot.pipeline.pin_composer import LICENSE_LABELS

        missing = [status for status in PUBLISHABLE_STATUSES if status not in LICENSE_LABELS]
        assert missing == []

    def test_blocked_statuses_fall_back_to_the_status_name(self):
        """Unknown/restricted statuses never publish, but must not crash if they do."""
        description = PinComposer().build_description(
            artwork(), "impressionism", evaluation(RightsStatus.RESTRICTED)
        )
        assert "License: restricted" in description


class TestCompose:
    def test_compose_produces_complete_metadata(self):
        meta = PinComposer().compose(
            artwork(), "impressionism", evaluation(RightsStatus.PUBLIC_DOMAIN), BOARD
        )
        assert meta.title.startswith("Water Lilies")
        assert "Impressionism" in meta.description
        assert meta.link == "https://www.metmuseum.org/art/1"
        assert meta.alt_text
        assert meta.board_target.board_id == "55"

    def test_description_respects_configured_limit(self):
        composer = PinComposer(description_max_length=40)
        meta = composer.compose(artwork(), "impressionism", evaluation(RightsStatus.CC0), BOARD)
        assert len(meta.description) == 40

    def test_description_includes_movement_and_collection(self):
        meta = PinComposer().compose(
            artwork(), "neo_impressionism", evaluation(RightsStatus.PUBLIC_DOMAIN), BOARD
        )
        assert "Neo Impressionism" in meta.description
        assert "The Metropolitan Museum of Art" in meta.description
        assert "License: Public domain" in meta.description

    def test_description_carries_search_keywords_exactly_once(self):
        meta = PinComposer().compose(
            artwork(), "impressionism", evaluation(RightsStatus.PUBLIC_DOMAIN), BOARD
        )
        hashtags = [token for token in meta.description.split() if token.startswith("#")]
        assert hashtags.count("#art") == 1, "keywords must not be duplicated"
        assert "#impressionism" in hashtags
        assert "#painting" in hashtags
        assert len(hashtags) == len(set(hashtags)), "no repeated hashtags"


class TestPublicCopyQuality:
    """Copy that reaches Pinterest must be clean, not museum-annotated."""

    def test_artist_decoration_is_stripped(self):
        meta = PinComposer().compose(
            artwork(artist_name="Gustave Courbet (French, 1819-1877)"),
            "realism",
            evaluation(RightsStatus.PUBLIC_DOMAIN),
            BOARD,
        )
        assert "Courbet" in meta.title
        assert "1819" not in meta.title
        assert "French" not in meta.title
        assert "French" not in meta.alt_text

    def test_artist_parenthetical_alias_is_removed(self):
        meta = PinComposer().compose(
            artwork(artist_name="Claude Lorrain (Claude Gellée)"),
            "baroque",
            evaluation(RightsStatus.PUBLIC_DOMAIN),
            BOARD,
        )
        assert "Claude Lorrain" in meta.title
        assert "Gellée" not in meta.title

    def test_alt_text_uses_the_clean_artist_name(self):
        meta = PinComposer().compose(
            artwork(artist_name="Edgar Degas (French, 1834-1917)"),
            "impressionism",
            evaluation(RightsStatus.PUBLIC_DOMAIN),
            BOARD,
        )
        assert "Edgar Degas" in meta.alt_text
        assert "1834" not in meta.alt_text

    def test_noisy_departments_do_not_become_hashtags(self):
        meta = PinComposer().compose(
            artwork(department="Modern European Painting and Sculpture"),
            "impressionism",
            evaluation(RightsStatus.PUBLIC_DOMAIN),
            BOARD,
        )
        hashtags = [token for token in meta.description.split() if token.startswith("#")]
        assert not any("moderneuropeanpainting" in tag for tag in hashtags)

    def test_short_departments_can_become_hashtags(self):
        meta = PinComposer().compose(
            artwork(department="Prints"),
            "impressionism",
            evaluation(RightsStatus.PUBLIC_DOMAIN),
            BOARD,
        )
        assert "#prints" in meta.description

    def test_hashtags_are_generated_once(self):
        from pathlib import Path

        from pinterest_art_bot.pipeline.pin_templates import load_pin_templates

        shipped = load_pin_templates(Path("config/pin_templates.yaml"))
        meta = PinComposer(text_config=shipped).compose(
            artwork(), "impressionism", evaluation(RightsStatus.PUBLIC_DOMAIN), BOARD
        )
        hashtags = [token for token in meta.description.split() if token.startswith("#")]
        assert hashtags
        assert len(hashtags) == len(set(hashtags)), f"duplicate hashtags in {hashtags}"

    def test_shipped_templates_render(self):
        from pathlib import Path

        from pinterest_art_bot.pipeline.pin_templates import load_pin_templates

        shipped = load_pin_templates(Path("config/pin_templates.yaml"))
        composer = PinComposer(text_config=shipped)
        meta = composer.compose(
            artwork(), "impressionism", evaluation(RightsStatus.PUBLIC_DOMAIN), BOARD
        )
        assert "Impressionist Art" in meta.title
        assert "License: Public domain" in meta.description
        assert "{" not in meta.description, "unrendered placeholder"
        assert "{" not in meta.title
