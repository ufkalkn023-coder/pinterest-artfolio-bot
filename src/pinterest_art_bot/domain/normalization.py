"""Shared text normalization.

Used wherever the same human-entered string must be compared across sources:
deduplication across museums and artist lookups. One implementation keeps those
decisions consistent.
"""

from __future__ import annotations

import re
import unicodedata

# Punctuation and separators that must not affect identity comparisons.
_PUNCTUATION = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")

# Leading articles and honorific-ish words dropped from titles.
_TITLE_NOISE = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "from",
        "with",
        "and",
        "at",
        "in",
        "on",
        "pour",
        "la",
        "le",
        "les",
        "el",
        "los",
        "las",
        "der",
        "die",
        "das",
    }
)

# Placeholder titles carry no identity: two different untitled works must not
# be considered the same work.
_PLACEHOLDER_TITLES = frozenset(
    {
        "untitled",
        "unknown",
        "no title",
        "without title",
        "titre inconnu",
        "sans titre",
        "sin titulo",
        "sin título",
        "senza titolo",
        "zonder titel",
    }
)

# Museums decorate artist names with dates, nationalities, and roles
# ("Monet, Claude (French, 1840-1926)", "Jackson Pollock American 1912").
# None of that is part of the artist's identity.
_PERSON_NOISE = re.compile(
    r"\b\d{3,4}\s*[-\u2013]\s*\d{0,4}\b"  # date ranges
    r"|\b\d{3,4}\b"  # single years
    r"|\b(?:american|british|english|french|italian|spanish|dutch|flemish|german|"
    r"austrian|swiss|belgian|russian|polish|danish|swedish|norwegian|mexican|"
    r"japanese|chinese|canadian|australian|scottish|welsh|irish|greek|hungarian|"
    r"czech|turkish|egyptian|iranian|indian|korean|portuguese|romanian|ukrainian)\b"
    r"|\b(?:artist|painter|printmaker|sculptor|engraver|draughtsman|draftsm)\b",
    re.IGNORECASE,
)

_PARENTHETICAL = re.compile(r"\(([^)]*)\)")


def strip_accents(value: str) -> str:
    """Remove diacritics so 'Müller' and 'Muller' compare equal."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_key(value: str) -> str:
    """Canonical lookup key: unaccented, punctuation-free, lowercased."""
    if not value:
        return ""
    without_punctuation = _PUNCTUATION.sub(" ", strip_accents(value).lower())
    return _WHITESPACE.sub(" ", without_punctuation).strip()


def normalize_person_name(value: str) -> str:
    """Normalize a person/artist name for cross-source comparison.

    Strips museum decoration (parentheticals, dates, nationalities, roles) and
    collapses to a single canonical form, so "Claude Lorrain (Claude Gellée)",
    "Claude Gellée", and "Gellée, Claude" all reduce to the same key.
    """
    if not value:
        return ""
    without_parentheses = _PARENTHETICAL.sub(" ", value)
    cleaned = _PERSON_NOISE.sub(" ", without_parentheses)

    # "Monet, Claude" and "Claude Monet" must agree, so try both orderings and
    # keep the one that looks canonical (single-token surname form first).
    head, separator, tail = cleaned.partition(",")
    if separator and tail.strip():
        reordered = f"{tail.strip()} {head.strip()}"
        # Prefer "surname given" ordering, which museums use most often.
        return normalize_key(reordered)

    return normalize_key(cleaned)


def normalize_title(value: str) -> str:
    """Normalize an artwork title for cross-source comparison.

    Case, punctuation, accents, and leading articles are all removed, so
    "The Night Watch" and "Night watch, the" match. Placeholder titles such as
    "Untitled" normalise to an empty string, because they carry no identity.
    """
    key = normalize_key(value)
    if not key:
        return ""
    words = [word for word in key.split() if word not in _TITLE_NOISE]
    normalized = " ".join(words) if words else key
    if normalized in _PLACEHOLDER_TITLES:
        return ""
    return normalized


def year_bucket(year: int | None, tolerance: int = 2) -> str | None:
    """Bucket a year to tolerate small catalogue discrepancies between museums."""
    if year is None:
        return None
    return str(int(year) // (tolerance * 2 + 1))
