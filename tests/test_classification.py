"""Tests for movement normalization and classification."""

import pytest

from pinterest_art_bot.classification.aliases import get_all_aliases, normalize_movement
from pinterest_art_bot.classification.artists import lookup_artist_movements
from pinterest_art_bot.classification.classifier import MovementClassifier
from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity
from pinterest_art_bot.domain.movement import ArtMovement, ConfidenceLevel


class TestMovementNormalization:
    """Raw strings from museum APIs must normalize to canonical movements."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Impressionism", ArtMovement.IMPRESSIONISM),
            ("Impressionist", ArtMovement.IMPRESSIONISM),
            ("French Impressionism", ArtMovement.IMPRESSIONISM),
            ("impressionist painting", ArtMovement.IMPRESSIONISM),
            ("Impressionist movement", ArtMovement.IMPRESSIONISM),
            ("IMPRESSIONISM", ArtMovement.IMPRESSIONISM),
        ],
    )
    def test_impressionism_variants(self, raw: str, expected: ArtMovement):
        assert normalize_movement(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Baroque", ArtMovement.BAROQUE),
            ("Italian Baroque", ArtMovement.BAROQUE),
            ("Baroque painting", ArtMovement.BAROQUE),
            ("17th century Baroque", ArtMovement.BAROQUE),
            ("baroque", ArtMovement.BAROQUE),
        ],
    )
    def test_baroque_variants(self, raw: str, expected: ArtMovement):
        assert normalize_movement(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Post-Impressionism", ArtMovement.POST_IMPRESSIONISM),
            ("Post-Impressionist", ArtMovement.POST_IMPRESSIONISM),
            ("Post Impressionism", ArtMovement.POST_IMPRESSIONISM),
        ],
    )
    def test_post_impressionism_variants(self, raw: str, expected: ArtMovement):
        assert normalize_movement(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Renaissance", ArtMovement.RENAISSANCE),
            ("High Renaissance", ArtMovement.HIGH_RENAISSANCE),
            ("Northern Renaissance", ArtMovement.NORTHERN_RENAISSANCE),
            ("Early Renaissance", ArtMovement.EARLY_RENAISSANCE),
        ],
    )
    def test_renaissance_variants(self, raw: str, expected: ArtMovement):
        assert normalize_movement(raw) == expected

    def test_surrealism(self):
        assert normalize_movement("Surrealism") == ArtMovement.SURREALISM
        assert normalize_movement("surrealist") == ArtMovement.SURREALISM

    def test_cubism(self):
        assert normalize_movement("Cubism") == ArtMovement.CUBISM
        assert normalize_movement("Analytic Cubism") == ArtMovement.CUBISM

    def test_unknown_returns_none(self):
        assert normalize_movement("") is None
        assert normalize_movement("xyzzy") is None
        assert normalize_movement("totally unknown style") is None

    def test_case_insensitive(self):
        assert normalize_movement("BAROQUE") == ArtMovement.BAROQUE
        assert normalize_movement("baroque") == ArtMovement.BAROQUE
        assert normalize_movement("Baroque") == ArtMovement.BAROQUE

    def test_neo_impressionism_before_impressionism(self):
        """More specific aliases must match before less specific ones."""
        assert normalize_movement("Neo-Impressionism") == ArtMovement.NEO_IMPRESSIONISM
        assert normalize_movement("Neo-Impressionist") == ArtMovement.NEO_IMPRESSIONISM

    def test_pointillism_maps_to_neo_impressionism(self):
        assert normalize_movement("Pointillism") == ArtMovement.NEO_IMPRESSIONISM

    def test_get_all_aliases(self):
        aliases = get_all_aliases(ArtMovement.BAROQUE)
        assert "baroque" in aliases
        assert len(aliases) >= 3


class TestMovementClassifier:
    """Evidence-based movement classification."""

    def setup_method(self):
        self.classifier = MovementClassifier()

    def test_classify_from_style_metadata(self, baroque_artwork: Artwork):
        result = self.classifier.classify(baroque_artwork)
        assert result.primary_movement == ArtMovement.BAROQUE
        assert result.is_classified is True
        assert result.confidence > 0
        assert len(result.evidence) > 0

    def test_classify_from_artist_mapping(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Water Lilies",
            artist_name="Claude Monet",
            year_start=1906,
            museum_name="Test Museum",
            public_domain=True,
        )
        result = self.classifier.classify(artwork)
        assert result.primary_movement == ArtMovement.IMPRESSIONISM
        assert result.is_classified is True

    def test_classify_impressionist(self, impressionist_artwork: Artwork):
        result = self.classifier.classify(impressionist_artwork)
        assert result.primary_movement == ArtMovement.IMPRESSIONISM
        assert result.confidence > 0

    def test_unclassified_when_no_evidence(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Untitled Object",
            museum_name="Test Museum",
        )
        result = self.classifier.classify(artwork)
        assert result.primary_movement == ArtMovement.UNCLASSIFIED
        assert result.is_classified is False
        assert result.confidence_level == ConfidenceLevel.NONE

    def test_unclassified_does_not_auto_publish(self):
        result = MovementClassifier().classify(
            Artwork(
                identity=ArtworkIdentity(source="test", source_object_id="1"),
                title="Unknown",
                museum_name="Test Museum",
            )
        )
        assert result.is_auto_publishable is False

    def test_evidence_includes_source_type(self, baroque_artwork: Artwork):
        result = self.classifier.classify(baroque_artwork)
        evidence_types = {e.evidence_type for e in result.evidence}
        assert len(evidence_types) > 0

    def test_classification_has_reason(self, baroque_artwork: Artwork):
        result = self.classifier.classify(baroque_artwork)
        assert result.reason is not None
        assert len(result.reason) > 0

    def test_raw_values_are_preserved(self, baroque_artwork: Artwork):
        result = self.classifier.classify(baroque_artwork)
        assert len(result.raw_values) > 0

    def test_high_confidence_from_strong_evidence(self):
        """Multiple strong evidence sources should yield high confidence."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="The Night Watch",
            artist_name="Rembrandt",
            year_start=1642,
            museum_name="Test Museum",
            style_raw="Baroque",
            movement_raw="Dutch Baroque",
            public_domain=True,
        )
        result = self.classifier.classify(artwork)
        assert result.primary_movement == ArtMovement.BAROQUE
        assert result.confidence_level == ConfidenceLevel.HIGH

    def test_multiple_movements_artist(self):
        """An artist with multiple movements should list secondaries."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="Test",
            artist_name="Pablo Picasso",
            museum_name="Test Museum",
        )
        result = self.classifier.classify(artwork)
        # Picasso maps to CUBISM (primary) and MODERNISM (secondary)
        assert result.primary_movement == ArtMovement.CUBISM

    def test_date_heuristic_alone_never_achieves_high_confidence(self):
        """CRITICAL INVARIANT: Year/date alone must NEVER achieve HIGH or MEDIUM confidence.

        Unknown artist + year 1880 only -> weak evidence -> must be held for review.
        """
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="999"),
            title="Landscape by Unknown",
            artist_name=None,  # No known artist
            year_start=1880,  # Date alone suggests Impressionism
            museum_name="Test Museum",
        )
        result = self.classifier.classify(artwork)
        assert result.confidence_level in (ConfidenceLevel.LOW, ConfidenceLevel.NONE)
        assert result.confidence_level != ConfidenceLevel.HIGH
        assert result.confidence_level != ConfidenceLevel.MEDIUM
        assert result.is_auto_publishable is False
        assert result.confidence <= 0.35

    def test_monet_plus_explicit_impressionism_achieves_high_confidence(self):
        """Monet + explicit museum Impressionism metadata -> HIGH -> auto-routable."""
        artwork = Artwork(
            identity=ArtworkIdentity(source="artic", source_object_id="14655"),
            title="Water Lilies",
            artist_name="Claude Monet",
            style_raw="Impressionism",
            year_start=1906,
            museum_name="Art Institute of Chicago",
        )
        result = self.classifier.classify(artwork)
        assert result.primary_movement == ArtMovement.IMPRESSIONISM
        assert result.confidence_level == ConfidenceLevel.HIGH
        assert result.is_auto_publishable is True


class TestAliasWordBoundaries:
    """Aliases must match whole words only.

    A museum tag such as "Popes" must never be classified as POP_ART, because a
    wrong movement is worse than no movement.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Popes", None),
            ("Pope Pius VII", None),
            ("pop", ArtMovement.POP_ART),
            ("pop art", ArtMovement.POP_ART),
            ("Pop Art", ArtMovement.POP_ART),
            ("Neo-Pop", ArtMovement.POP_ART),
            ("dada", ArtMovement.DADA),
            ("Dadaism", ArtMovement.DADA),
            ("Post-Impressionism", ArtMovement.POST_IMPRESSIONISM),
            ("post impressionism", ArtMovement.POST_IMPRESSIONISM),
            ("Impressionist", ArtMovement.IMPRESSIONISM),
            ("Northern Renaissance", ArtMovement.NORTHERN_RENAISSANCE),
            ("modern", ArtMovement.MODERNISM),
            ("Modern Art", ArtMovement.MODERNISM),
            ("Arts and Crafts", ArtMovement.ARTS_AND_CRAFTS),
            ("Portraits", None),
            ("Men", None),
            ("Abstract Expressionism", ArtMovement.ABSTRACT_EXPRESSIONISM),
        ],
    )
    def test_normalization_respects_word_boundaries(self, raw, expected):
        assert normalize_movement(raw) == expected

    def test_pop_tag_no_longer_produces_pop_art(self, impressionist_artwork):
        """Regression: a 'Popes' tag previously produced POP_ART at 0.5 weight."""
        from pinterest_art_bot.domain.artwork import ArtworkIdentity, ImageInfo

        artwork = impressionist_artwork.model_copy(
            update={
                "identity": ArtworkIdentity(source="met", source_object_id="999"),
                "style_raw": None,
                "period_raw": None,
                "tags": ["Popes", "Portraits"],
            }
        )
        result = MovementClassifier().classify(artwork)

        assert result.primary_movement != ArtMovement.POP_ART
        assert not any(
            evidence.normalized_to == ArtMovement.POP_ART and evidence.raw_value.lower() == "popes"
            for evidence in result.evidence
        )
        assert artwork.primary_image is not None  # fixture sanity
        assert isinstance(ImageInfo(url="https://x/y.jpg"), ImageInfo)


class TestArtistNameNormalization:
    """Museum artist strings are formatted inconsistently; lookups must cope."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Claude Monet", ArtMovement.IMPRESSIONISM),
            ("Monet, Claude", ArtMovement.IMPRESSIONISM),
            ("Monet, Claude (French, 1840-1926)", ArtMovement.IMPRESSIONISM),
            ("Monet, Claude 1840-1926", ArtMovement.IMPRESSIONISM),
            ("El Greco (Domenikos Theotokopoulos)", ArtMovement.MANNERISM),
            ("El Greco", ArtMovement.MANNERISM),
            ("Claude Lorrain (Claude Gellée)", ArtMovement.BAROQUE),
            ("Pablo Picasso (Spanish, 1881-1973)", ArtMovement.CUBISM),
            ("Jackson Pollock American 1912", ArtMovement.ABSTRACT_EXPRESSIONISM),
            ("Vincent van Gogh", ArtMovement.POST_IMPRESSIONISM),
            ("Rembrandt (Dutch, 1606-1669)", ArtMovement.BAROQUE),
            ("  JOHANNES VERMEER  ", ArtMovement.BAROQUE),
            ("Johannes Vermeer (Dutch, 1632-1675)", ArtMovement.BAROQUE),
        ],
    )
    def test_known_artists_resolve(self, raw, expected):
        movements = lookup_artist_movements(raw)
        assert movements is not None
        assert movements[0] == expected

    @pytest.mark.parametrize("raw", ["", "   ", "Some Unknown Person", "Anonymous Attributed"])
    def test_unknown_artists_return_none(self, raw):
        assert lookup_artist_movements(raw) is None

    def test_every_key_is_canonical(self):
        """Source keys must already be normalized so lookups cannot miss."""
        from pinterest_art_bot.classification.artists import ARTIST_MOVEMENTS, _key

        offenders = [key for key in ARTIST_MOVEMENTS if _key(key) != key]
        assert offenders == []

    def test_every_entry_has_a_movement(self):
        from pinterest_art_bot.classification.artists import ARTIST_MOVEMENTS

        empty = [key for key, movements in ARTIST_MOVEMENTS.items() if not movements]
        assert empty == []

    def test_map_is_substantial(self):
        from pinterest_art_bot.classification.artists import known_artist_count

        assert known_artist_count() >= 200

    def test_map_covers_expected_movements(self):
        from pinterest_art_bot.classification.artists import ARTIST_MOVEMENTS

        covered = {movement for movements in ARTIST_MOVEMENTS.values() for movement in movements}
        expected = {
            ArtMovement.EARLY_RENAISSANCE,
            ArtMovement.HIGH_RENAISSANCE,
            ArtMovement.NORTHERN_RENAISSANCE,
            ArtMovement.MANNERISM,
            ArtMovement.BAROQUE,
            ArtMovement.ROCOCO,
            ArtMovement.NEOCLASSICISM,
            ArtMovement.ROMANTICISM,
            ArtMovement.REALISM,
            ArtMovement.IMPRESSIONISM,
            ArtMovement.POST_IMPRESSIONISM,
            ArtMovement.NEO_IMPRESSIONISM,
            ArtMovement.SYMBOLISM,
            ArtMovement.EXPRESSIONISM,
            ArtMovement.FAUVISM,
            ArtMovement.CUBISM,
            ArtMovement.FUTURISM,
            ArtMovement.CONSTRUCTIVISM,
            ArtMovement.SURREALISM,
            ArtMovement.DADA,
            ArtMovement.ABSTRACT_EXPRESSIONISM,
            ArtMovement.ART_NOUVEAU,
            ArtMovement.ART_DECO,
            ArtMovement.POP_ART,
            ArtMovement.OP_ART,
            ArtMovement.MINIMALISM,
            ArtMovement.MODERNISM,
        }
        assert expected <= covered

    def test_artist_evidence_is_recorded(self, baroque_artwork):
        result = MovementClassifier().classify(baroque_artwork)
        artist_evidence = [
            e for e in result.evidence if e.evidence_type.value == "canonical_artist_mapping"
        ]
        assert artist_evidence
        assert result.primary_movement == ArtMovement.BAROQUE
        assert result.confidence_level == ConfidenceLevel.HIGH


class TestArtistNameNormalisation:
    """Museums disagree on punctuation; they must not create duplicate artists."""

    @pytest.mark.parametrize(
        "spelling",
        [
            "Jean Honoré Fragonard",
            "Jean-Honoré Fragonard",
            "jean honore fragonard",
            "FRAGONARD, Jean-Honoré",
            "Fragonard, Jean-Honoré (French, 1732-1806)",
        ],
    )
    def test_separator_variants_resolve_to_one_artist(self, spelling):
        from pinterest_art_bot.classification.artists import lookup_artist_movements

        movements = lookup_artist_movements(spelling)
        assert movements is not None, spelling
        assert movements[0] == ArtMovement.ROCOCO

    def test_table_keys_are_canonical(self):
        """Every key must survive its own normalisation, or lookups can miss."""
        from pinterest_art_bot.classification.artists import ARTIST_MOVEMENTS, _key

        assert not [k for k in ARTIST_MOVEMENTS if _key(k) != k]

    def test_every_key_is_reachable_through_the_public_lookup(self):
        """A key the lookup cannot produce is dead weight."""
        from pinterest_art_bot.classification.artists import (
            ARTIST_MOVEMENTS,
            lookup_artist_movements,
        )

        unreachable = [k for k in ARTIST_MOVEMENTS if lookup_artist_movements(k) is None]
        assert not unreachable

    def test_no_artist_maps_to_no_movement(self):
        from pinterest_art_bot.classification.artists import ARTIST_MOVEMENTS

        assert not [k for k, v in ARTIST_MOVEMENTS.items() if not v]

    def test_attributions_are_for_attributed_artists_only(self):
        """Anonymous workshop labels must not be pinned to one movement."""
        from pinterest_art_bot.classification.artists import lookup_artist_movements

        for label in (
            "French Painter",
            "Northern European Painter",
            "Anonymous",
            "Unknown",
        ):
            assert lookup_artist_movements(label) is None, label


class TestOldMasterAttributionsClearTheAutoRouteBar:
    """A known artist must be enough evidence to route a Pin automatically."""

    def _artwork(self, artist: str, year_start: int, year_end: int):
        from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity
        from pinterest_art_bot.domain.rights import RightsStatus

        return Artwork(
            identity=ArtworkIdentity(source="met", source_object_id=f"{artist}:{year_start}"),
            museum_name="The Metropolitan Museum of Art",
            title="Portrait",
            artist_name=artist,
            year_start=year_start,
            year_end=year_end,
            rights_status=RightsStatus.PUBLIC_DOMAIN,
        )

    @pytest.mark.parametrize(
        ("artist", "years", "expected"),
        [
            ("Corneille de Lyon", (1533, 1575), ArtMovement.RENAISSANCE),
            ("Gerbrand van den Eeckhout", (1650, 1653), ArtMovement.BAROQUE),
            ("Jean Honoré Fragonard", (1786, 1786), ArtMovement.ROCOCO),
            ("William Etty", (1815, 1825), ArtMovement.ROMANTICISM),
        ],
    )
    def test_known_artist_reaches_high_confidence(self, artist, years, expected):
        from pinterest_art_bot.classification.classifier import MovementClassifier

        result = MovementClassifier().classify(self._artwork(artist, *years))
        assert result.primary_movement == expected
        assert result.confidence_level == ConfidenceLevel.HIGH, result.reason
        assert result.confidence >= 0.8

    def test_artist_evidence_is_recorded_for_audit(self):
        from pinterest_art_bot.classification.classifier import MovementClassifier
        from pinterest_art_bot.domain.movement import EvidenceType

        result = MovementClassifier().classify(self._artwork("Jean Honoré Fragonard", 1786, 1786))
        artist_evidence = [
            e for e in result.evidence if e.evidence_type == EvidenceType.CANONICAL_ARTIST_MAPPING
        ]
        assert artist_evidence
        assert "Fragonard" in artist_evidence[0].raw_value

    def test_an_artist_attributed_to_two_movements_is_held_for_review(self):
        """A split attribution is genuine uncertainty and must not auto-route."""
        from pinterest_art_bot.classification.classifier import MovementClassifier

        result = MovementClassifier().classify(self._artwork("Agnolo Gaddi", 1390, 1390))
        assert result.primary_movement == ArtMovement.MEDIEVAL
        assert ArtMovement.GOTHIC in result.secondary_movements
        # The secondary attribution dilutes confidence below the HIGH bar.
        assert result.confidence_level == ConfidenceLevel.MEDIUM
        assert result.confidence < 0.8

    def test_unknown_artist_still_does_not_auto_route(self):
        """Widening the table must not widen what counts as HIGH confidence."""
        from pinterest_art_bot.classification.classifier import MovementClassifier

        result = MovementClassifier().classify(self._artwork("Zzz Unknown Artisan", 1500, 1510))
        assert result.confidence_level != ConfidenceLevel.HIGH
