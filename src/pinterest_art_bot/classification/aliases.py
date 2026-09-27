"""Movement alias / normalization layer.

This module maps the messy real-world strings from museum APIs into
our canonical ArtMovement enum values.

Design:
  - All matching is case-insensitive.
  - Aliases are checked as substrings, so "French Impressionism"
    matches the alias "impressionism".
  - More specific aliases are checked before less specific ones
    (longer strings first within the same movement).
  - New aliases can be added without changing any business logic.

This is the ONLY place where raw string → movement mapping happens.
No other module should contain ad-hoc string comparisons for movements.
"""

from __future__ import annotations

import re

from pinterest_art_bot.domain.movement import ArtMovement

# ── Alias mapping: raw string (lowercase) → canonical movement ──────
# Order within each movement: most specific first.
# At lookup time we sort all aliases by length descending to ensure
# "post-impressionism" matches before "impressionism".

_MOVEMENT_ALIASES: dict[str, ArtMovement] = {}


def _register(movement: ArtMovement, *aliases: str) -> None:
    """Register aliases for a movement (case-insensitive)."""
    for alias in aliases:
        _MOVEMENT_ALIASES[alias.lower()] = movement


# ── Ancient & Medieval ──────────────────────────────────────────────
_register(
    ArtMovement.ANCIENT,
    "ancient",
    "antiquity",
    "classical antiquity",
    "ancient greek",
    "ancient roman",
    "ancient egyptian",
    "hellenistic",
)
_register(
    ArtMovement.MEDIEVAL,
    "medieval",
    "middle ages",
    "romanesque",
    "early medieval",
    "carolingian",
    "ottonian",
)
_register(ArtMovement.GOTHIC, "gothic", "international gothic", "gothic art", "late gothic")
_register(ArtMovement.BYZANTINE, "byzantine", "byzantine art", "eastern orthodox")

# ── Renaissance family ──────────────────────────────────────────────
_register(
    ArtMovement.EARLY_RENAISSANCE,
    "early renaissance",
    "proto-renaissance",
    "trecento",
    "quattrocento",
)
_register(ArtMovement.HIGH_RENAISSANCE, "high renaissance", "cinquecento")
_register(
    ArtMovement.NORTHERN_RENAISSANCE,
    "northern renaissance",
    "flemish renaissance",
    "german renaissance",
    "dutch renaissance",
)
_register(ArtMovement.RENAISSANCE, "renaissance", "italian renaissance", "renaissance art")
_register(ArtMovement.MANNERISM, "mannerism", "mannerist", "late renaissance")

# ── 17th-18th century ──────────────────────────────────────────────
_register(
    ArtMovement.BAROQUE,
    "baroque",
    "italian baroque",
    "spanish baroque",
    "flemish baroque",
    "dutch baroque",
    "baroque art",
    "baroque painting",
    "17th century baroque",
)
_register(ArtMovement.ROCOCO, "rococo", "late baroque", "rocaille")

# ── 18th-19th century ──────────────────────────────────────────────
_register(
    ArtMovement.NEOCLASSICISM,
    "neoclassicism",
    "neoclassical",
    "neo-classicism",
    "neo-classical",
    "neoclassicist",
)
_register(ArtMovement.ROMANTICISM, "romanticism", "romantic", "romantic movement", "romantic art")
_register(ArtMovement.REALISM, "realism", "realist", "social realism", "naturalism", "naturalist")

# ── Impressionism family ───────────────────────────────────────────
_register(
    ArtMovement.NEO_IMPRESSIONISM,
    "neo-impressionism",
    "neo-impressionist",
    "pointillism",
    "pointillist",
    "divisionism",
    "divisionist",
)
_register(
    ArtMovement.POST_IMPRESSIONISM,
    "post-impressionism",
    "post-impressionist",
    "post impressionism",
    "post impressionist",
)
_register(
    ArtMovement.IMPRESSIONISM,
    "impressionism",
    "impressionist",
    "french impressionism",
    "impressionist painting",
    "impressionist movement",
)

# ── Late 19th / early 20th ─────────────────────────────────────────
_register(ArtMovement.SYMBOLISM, "symbolism", "symbolist")
_register(
    ArtMovement.ART_NOUVEAU,
    "art nouveau",
    "jugendstil",
    "stile liberty",
    "modernisme",
    "secession",
    "secessionist",
)
_register(
    ArtMovement.ARTS_AND_CRAFTS, "arts and crafts", "arts & crafts", "arts and crafts movement"
)

# ── Early 20th-century avant-garde ─────────────────────────────────
_register(
    ArtMovement.EXPRESSIONISM,
    "expressionism",
    "expressionist",
    "german expressionism",
    "die brücke",
    "der blaue reiter",
)
_register(ArtMovement.FAUVISM, "fauvism", "fauvist", "les fauves")
_register(
    ArtMovement.CUBISM,
    "cubism",
    "cubist",
    "analytic cubism",
    "synthetic cubism",
    "analytical cubism",
)
_register(ArtMovement.FUTURISM, "futurism", "futurist", "italian futurism")
_register(ArtMovement.CONSTRUCTIVISM, "constructivism", "constructivist", "russian constructivism")

# ── Interwar ────────────────────────────────────────────────────────
_register(ArtMovement.DADA, "dada", "dadaism", "dadaist")
_register(ArtMovement.SURREALISM, "surrealism", "surrealist", "surrealist art")
_register(ArtMovement.ART_DECO, "art deco", "art déco")

# ── Post-war ────────────────────────────────────────────────────────
_register(
    ArtMovement.ABSTRACT_EXPRESSIONISM,
    "abstract expressionism",
    "abstract expressionist",
    "action painting",
    "color field",
    "color field painting",
    "new york school",
)
_register(ArtMovement.POP_ART, "pop art", "pop", "neo-pop")
_register(ArtMovement.MINIMALISM, "minimalism", "minimalist", "minimal art")
_register(ArtMovement.OP_ART, "op art", "optical art", "kinetic art")

# ── Broad categories ───────────────────────────────────────────────
_register(ArtMovement.MODERNISM, "modernism", "modernist", "modern art", "modern")
_register(
    ArtMovement.CONTEMPORARY, "contemporary", "contemporary art", "postmodern", "postmodernism"
)

# ── Pre-sort aliases by length (longest first for greedy matching) ──
_SORTED_ALIASES: list[tuple[str, ArtMovement]] = sorted(
    _MOVEMENT_ALIASES.items(),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

# Aliases are matched on word boundaries, not as raw substrings, so a museum tag
# such as "Popes" cannot be classified as POP_ART just because "pop" is inside it.
_WORD_BOUNDARY_CHARS = re.compile(r"[^a-z0-9]+")


def _word_pattern(alias: str) -> re.Pattern[str]:
    """Compile a word-boundary pattern for an alias.

    Separators in the input (hyphens, slashes, underscores) are treated as word
    boundaries, so "post-impressionism" still matches the "post impressionism" alias.
    """
    parts = [re.escape(part) for part in _WORD_BOUNDARY_CHARS.split(alias) if part]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-_/]+".join(parts) + r"(?![a-z0-9])")


_ALIAS_PATTERNS: list[tuple[re.Pattern[str], ArtMovement]] = [
    (_word_pattern(alias), movement) for alias, movement in _SORTED_ALIASES
]


def normalize_movement(raw: str) -> ArtMovement | None:
    """Normalize a raw movement/style string to a canonical ArtMovement.

    Returns None if no alias matches. Does NOT default to UNCLASSIFIED —
    that decision belongs to the classifier, not the normalizer.

    Matching strategy:
      1. Exact match (case-insensitive)
      2. Word-boundary match, longest alias first

    Word boundaries matter: a naive substring match classifies museum tags such
    as "Popes" as POP_ART because "pop" is a substring. A wrong movement is
    worse than no movement, so ambiguous matches are rejected.
    """
    cleaned = raw.lower().strip()
    if not cleaned:
        return None

    # Exact match
    exact = _MOVEMENT_ALIASES.get(cleaned)
    if exact is not None:
        return exact

    # Word-boundary match (longest alias wins)
    for pattern, movement in _ALIAS_PATTERNS:
        if pattern.search(cleaned):
            return movement

    return None


def get_all_aliases(movement: ArtMovement) -> list[str]:
    """Return all registered aliases for a movement."""
    return [alias for alias, m in _MOVEMENT_ALIASES.items() if m == movement]


def get_alias_count() -> int:
    """Total number of registered aliases."""
    return len(_MOVEMENT_ALIASES)
