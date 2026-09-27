"""Art movement domain model.

Provides the canonical movement taxonomy, confidence levels, and
structured classification results with evidence tracking.

Design principles:
  - Movements are a flat enum — easy to add, rename, alias, disable.
  - Classification always carries evidence and confidence.
  - UNCLASSIFIED is a first-class state, not an error.
  - Wrong classification is worse than skipping an artwork.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ArtMovement(StrEnum):
    """Canonical art movement taxonomy.

    These are internal identifiers, not display names.
    Display names are handled by board configuration.
    """

    # Ancient & Medieval
    ANCIENT = "ancient"
    MEDIEVAL = "medieval"
    GOTHIC = "gothic"
    BYZANTINE = "byzantine"

    # Renaissance family
    EARLY_RENAISSANCE = "early_renaissance"
    RENAISSANCE = "renaissance"
    HIGH_RENAISSANCE = "high_renaissance"
    NORTHERN_RENAISSANCE = "northern_renaissance"
    MANNERISM = "mannerism"

    # 17th-18th century
    BAROQUE = "baroque"
    ROCOCO = "rococo"

    # 18th-19th century
    NEOCLASSICISM = "neoclassicism"
    ROMANTICISM = "romanticism"
    REALISM = "realism"

    # Impressionism family
    IMPRESSIONISM = "impressionism"
    POST_IMPRESSIONISM = "post_impressionism"
    NEO_IMPRESSIONISM = "neo_impressionism"

    # Late 19th / early 20th
    SYMBOLISM = "symbolism"
    ART_NOUVEAU = "art_nouveau"
    ARTS_AND_CRAFTS = "arts_and_crafts"

    # Early 20th-century avant-garde
    EXPRESSIONISM = "expressionism"
    FAUVISM = "fauvism"
    CUBISM = "cubism"
    FUTURISM = "futurism"
    CONSTRUCTIVISM = "constructivism"

    # Interwar
    DADA = "dada"
    SURREALISM = "surrealism"
    ART_DECO = "art_deco"

    # Post-war
    ABSTRACT_EXPRESSIONISM = "abstract_expressionism"
    POP_ART = "pop_art"
    MINIMALISM = "minimalism"
    OP_ART = "op_art"

    # Broad categories
    MODERNISM = "modernism"
    CONTEMPORARY = "contemporary"

    # Catch-all
    UNCLASSIFIED = "unclassified"


class ConfidenceLevel(StrEnum):
    """Classification confidence tiers.

    HIGH   → eligible for automatic board routing and publication.
    MEDIUM → configurable; may auto-route or require review.
    LOW    → do NOT auto-publish. Skip or queue for manual review.
    NONE   → no classification evidence found.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class EvidenceType(StrEnum):
    """What type of evidence contributed to the classification."""

    MUSEUM_MOVEMENT_FIELD = "museum_movement_field"
    MUSEUM_STYLE_FIELD = "museum_style_field"
    MUSEUM_PERIOD_FIELD = "museum_period_field"
    MUSEUM_CLASSIFICATION_FIELD = "museum_classification_field"
    MUSEUM_TAGS = "museum_tags"
    CANONICAL_ARTIST_MAPPING = "canonical_artist_mapping"
    DATE_RANGE_HEURISTIC = "date_range_heuristic"
    CULTURE_MAPPING = "culture_mapping"
    LLM_CLASSIFICATION = "llm_classification"


class ClassificationEvidence(BaseModel):
    """A single piece of evidence for a movement classification."""

    evidence_type: EvidenceType
    raw_value: str
    normalized_to: ArtMovement
    weight: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="How much weight this evidence carries (0.0-1.0).",
    )
    note: str | None = None


class MovementClassification(BaseModel):
    """Structured classification result for an artwork.

    Always includes evidence so decisions can be audited.
    """

    primary_movement: ArtMovement = ArtMovement.UNCLASSIFIED
    secondary_movements: list[ArtMovement] = Field(default_factory=list)
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Overall confidence score (0.0-1.0).",
    )
    confidence_level: ConfidenceLevel = ConfidenceLevel.NONE
    evidence: list[ClassificationEvidence] = Field(default_factory=list)
    raw_values: list[str] = Field(
        default_factory=list,
        description="All raw style/movement/period values from the source.",
    )
    reason: str | None = Field(
        default=None,
        description="Human-readable classification rationale.",
    )

    @property
    def is_classified(self) -> bool:
        return self.primary_movement != ArtMovement.UNCLASSIFIED

    @property
    def is_auto_publishable(self) -> bool:
        """Whether confidence is high enough for automatic board routing."""
        return self.confidence_level == ConfidenceLevel.HIGH

    @staticmethod
    def unclassified(raw_values: list[str] | None = None) -> MovementClassification:
        """Factory for an explicit unclassified result."""
        return MovementClassification(
            primary_movement=ArtMovement.UNCLASSIFIED,
            confidence=0.0,
            confidence_level=ConfidenceLevel.NONE,
            raw_values=raw_values or [],
            reason="No classification evidence found.",
        )
