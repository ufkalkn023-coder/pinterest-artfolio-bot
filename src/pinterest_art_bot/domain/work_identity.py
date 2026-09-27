"""Cross-source work identity.

Two museums routinely hold different records — and different photographs — of
the same artwork, so neither the canonical ID nor the image hash can detect the
duplication. This module answers "is this the same work?" conservatively.

A match requires ALL of:
  - a meaningful (non-placeholder) title on both sides,
  - strong title agreement (token overlap),
  - a shared artist token on both sides,
  - compatible dates when both sides know them.

Anything less is reported as a different work. False merges are far more
damaging than missed duplicates: a wrong merge hides an artwork entirely, while
a missed duplicate costs one extra Pin.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.normalization import (
    normalize_person_name,
    normalize_title,
)

# Minimum share of title tokens two records must share.
TITLE_OVERLAP_THRESHOLD = 0.8
# Titles shorter than this are too generic to trust.
MIN_TITLE_TOKENS = 2
# Allowed difference in reported dates.
YEAR_TOLERANCE = 5

_STOP_TOKENS = frozenset({"after", "before", "versus", "vs", "sans", "with"})


@dataclass(frozen=True)
class WorkIdentity:
    """Comparable identity of a single artwork, independent of its museum."""

    artist_tokens: frozenset[str]
    title_tokens: frozenset[str]
    year: int | None = None

    @property
    def is_comparable(self) -> bool:
        """Whether this identity carries enough signal to compare at all."""
        return bool(self.artist_tokens) and len(self.title_tokens) >= MIN_TITLE_TOKENS

    @property
    def key(self) -> str:
        """Deterministic hash, used as a fast-path equality lookup."""
        payload = "|".join(
            [
                ",".join(sorted(self.artist_tokens)),
                ",".join(sorted(self.title_tokens)),
                str(self.year or ""),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def work_identity(artwork: Artwork) -> WorkIdentity:
    """Derive a comparable identity from an artwork's metadata."""
    artist = normalize_person_name(artwork.artist_name or "")
    title = normalize_title(artwork.title)

    artist_tokens = frozenset(token for token in artist.split() if token not in _STOP_TOKENS)
    title_tokens = frozenset(token for token in title.split() if token not in _STOP_TOKENS)

    year = artwork.year_start if artwork.year_start is not None else artwork.year_end
    return WorkIdentity(artist_tokens=artist_tokens, title_tokens=title_tokens, year=year)


def title_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Share of title tokens present in both records."""
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return shared / max(len(left), len(right))


def titles_agree(left: frozenset[str], right: frozenset[str]) -> bool:
    """Whether two titles describe the same work.

    Museums append subtitles inconsistently, so a shorter title fully contained
    in a longer one is treated as agreement ("Water Lilies" vs
    "Water Lilies at Giverny"). Otherwise the token overlap must clear the
    threshold.
    """
    if len(left) < MIN_TITLE_TOKENS or len(right) < MIN_TITLE_TOKENS:
        return False
    if left <= right or right <= left:
        return True
    return title_overlap(left, right) >= TITLE_OVERLAP_THRESHOLD


def is_same_work(candidate: WorkIdentity, existing: WorkIdentity) -> bool:
    """Whether two identities describe the same work."""
    if not candidate.is_comparable or not existing.is_comparable:
        return False

    if not (candidate.artist_tokens & existing.artist_tokens):
        return False

    if not titles_agree(candidate.title_tokens, existing.title_tokens):
        return False

    candidate_year = candidate.year
    existing_year = existing.year
    years_conflict = (
        candidate_year is not None
        and existing_year is not None
        and abs(candidate_year - existing_year) > YEAR_TOLERANCE
    )
    return not years_conflict


def is_same_work_key(candidate: WorkIdentity, existing_key: str) -> bool:
    """Fast-path equality check against a stored fingerprint."""
    return candidate.key == existing_key
