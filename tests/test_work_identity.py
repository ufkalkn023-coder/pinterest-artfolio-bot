"""Tests for cross-source work identity matching.

The rule under test: never merge two records unless the evidence is strong.
A wrong merge hides an artwork; a missed duplicate costs one extra Pin.
"""

import pytest

from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity
from pinterest_art_bot.domain.normalization import (
    normalize_person_name,
    normalize_title,
    strip_accents,
    year_bucket,
)
from pinterest_art_bot.domain.work_identity import (
    MIN_TITLE_TOKENS,
    WorkIdentity,
    is_same_work,
    title_overlap,
    titles_agree,
    work_identity,
)


def artwork(
    *,
    title: str = "The Dance Class",
    artist: str | None = "Edgar Degas",
    year: int | None = 1874,
) -> Artwork:
    return Artwork(
        identity=ArtworkIdentity(source="met", source_object_id="1"),
        title=title,
        artist_name=artist,
        museum_name="Test Museum",
        year_start=year,
    )


class TestNormalization:
    def test_strip_accents(self):
        assert strip_accents("Claude Gellée") == "Claude Gellee"

    def test_person_name_drops_dates_and_nationality(self):
        assert normalize_person_name("Claude Monet (French, 1840-1926)") == "claude monet"
        assert normalize_person_name("Jackson Pollock American 1912") == "jackson pollock"

    def test_person_name_handles_reversed_order(self):
        assert normalize_person_name("Monet, Claude") == "claude monet"

    def test_title_drops_articles_and_punctuation(self):
        assert normalize_title("The Night Watch") == "night watch"
        assert normalize_title("Night watch, the!") == "night watch"

    @pytest.mark.parametrize("placeholder", ["Untitled", "untitled", "Sans titre", "No Title"])
    def test_placeholder_titles_normalize_to_empty(self, placeholder):
        assert normalize_title(placeholder) == ""

    def test_year_bucket_is_narrow_by_design(self):
        """The bucket feeds a fast-path hash, so it errs towards not matching.

        Near-identical years may fall in the same bucket; far-apart years never
        do. Ambiguous near cases are resolved by is_same_work's tolerance check.
        """
        assert year_bucket(1874) == year_bucket(1873)
        assert year_bucket(1874) != year_bucket(1900)

    def test_year_bucket_of_unknown_year(self):
        assert year_bucket(None) is None


class TestComparability:
    def test_full_record_is_comparable(self):
        assert work_identity(artwork()).is_comparable is True

    def test_placeholder_title_is_not_comparable(self):
        assert work_identity(artwork(title="Untitled")).is_comparable is False

    def test_missing_artist_is_not_comparable(self):
        assert work_identity(artwork(artist=None)).is_comparable is False

    def test_single_word_title_is_not_comparable(self):
        assert work_identity(artwork(title="Aquarium")).is_comparable is False

    def test_comparable_requires_minimum_title_tokens(self):
        identity = WorkIdentity(
            artist_tokens=frozenset({"monet"}), title_tokens=frozenset({"a", "b"})
        )
        assert len(identity.title_tokens) >= MIN_TITLE_TOKENS
        assert identity.is_comparable is True


class TestTitleAgreement:
    def test_identical_titles_agree(self):
        tokens = frozenset({"dance", "class"})
        assert titles_agree(tokens, tokens) is True

    def test_shorter_title_contained_in_longer_agrees(self):
        assert (
            titles_agree(frozenset({"dance", "class"}), frozenset({"dance", "class", "1874"}))
            is True
        )

    def test_different_titles_disagree(self):
        assert (
            titles_agree(frozenset({"water", "lilies"}), frozenset({"haystacks", "snow"})) is False
        )

    def test_overlap_is_measured_against_the_longer_title(self):
        assert title_overlap(frozenset({"a", "b"}), frozenset({"a", "b", "c"})) == pytest.approx(
            2 / 3
        )

    def test_empty_titles_never_agree(self):
        assert titles_agree(frozenset(), frozenset({"a", "b"})) is False


class TestSameWork:
    def test_same_work_with_different_record_shape(self):
        met = work_identity(artwork(title="Dance Class", artist="Edgar Degas", year=1872))
        cleveland = work_identity(
            artwork(title="The Dance Class", artist="Edgar Degas (French, 1834-1917)", year=1874)
        )
        assert is_same_work(met, cleveland) is True

    def test_different_artist_is_a_different_work(self):
        first = work_identity(artwork(artist="Edgar Degas"))
        second = work_identity(artwork(artist="Mary Cassatt"))
        assert is_same_work(first, second) is False

    def test_completely_different_title_is_a_different_work(self):
        first = work_identity(artwork(title="The Dance Class"))
        second = work_identity(artwork(title="Water Lilies at Sunset"))
        assert is_same_work(first, second) is False

    def test_year_far_apart_is_a_different_work(self):
        first = work_identity(artwork(year=1874))
        second = work_identity(artwork(year=1960))
        assert is_same_work(first, second) is False

    def test_year_within_tolerance_is_the_same_work(self):
        first = work_identity(artwork(year=1874))
        second = work_identity(artwork(year=1876))
        assert is_same_work(first, second) is True

    def test_unknown_year_does_not_block_a_match(self):
        first = work_identity(artwork(year=1874))
        second = work_identity(artwork(year=None))
        assert is_same_work(first, second) is True

    def test_incomparable_records_never_match(self):
        first = work_identity(artwork(title="Untitled", artist=None))
        second = work_identity(artwork(title="Untitled", artist=None))
        assert is_same_work(first, second) is False

    def test_key_is_deterministic_and_sensitive(self):
        first = work_identity(artwork())
        second = work_identity(artwork())
        other = work_identity(artwork(year=1875))
        assert first.key == second.key
        assert first.key != other.key

    def test_symmetric(self):
        first = work_identity(artwork(title="Dance Class", artist="Edgar Degas", year=1872))
        second = work_identity(artwork(artist="Edgar Degas (French, 1834-1917)"))
        assert is_same_work(first, second) == is_same_work(second, first)
