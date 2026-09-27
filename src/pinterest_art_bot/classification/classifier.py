"""Movement classifier — evidence-based artwork classification.

The classifier examines artwork metadata through a prioritized evidence
pipeline and produces a structured MovementClassification with confidence
and full evidence trail.

Priority order:
  1. Explicit museum metadata (movement, style, period, tags)
  2. Known canonical artist mappings
  3. Date range + contextual heuristics
  4. (Future) LLM-based classification

Key invariants:
  - Classification ALWAYS carries evidence.
  - UNCLASSIFIED is a valid, first-class result.
  - Wrong classification is worse than UNCLASSIFIED.
  - Confidence determines eligibility for auto-routing.
"""

from __future__ import annotations

from collections import defaultdict

import structlog

from pinterest_art_bot.classification.aliases import normalize_movement
from pinterest_art_bot.classification.artists import lookup_artist_movements
from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.movement import (
    ArtMovement,
    ClassificationEvidence,
    ConfidenceLevel,
    EvidenceType,
    MovementClassification,
)

logger = structlog.get_logger()

# Confidence thresholds
HIGH_CONFIDENCE_THRESHOLD = 0.8
MEDIUM_CONFIDENCE_THRESHOLD = 0.5
LOW_CONFIDENCE_THRESHOLD = 0.3


class MovementClassifier:
    """Evidence-based art movement classifier.

    Examines artwork metadata through prioritized evidence sources
    and produces structured classification with confidence scoring.
    """

    def __init__(
        self,
        *,
        auto_threshold: float = HIGH_CONFIDENCE_THRESHOLD,
        minimum_threshold: float = LOW_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._auto_threshold = auto_threshold
        self._minimum_threshold = minimum_threshold

    def classify(self, artwork: Artwork) -> MovementClassification:
        """Classify an artwork's movement based on available evidence.

        Returns a MovementClassification with evidence, confidence,
        and confidence level.
        """
        evidence: list[ClassificationEvidence] = []
        raw_values: list[str] = []

        # ── Priority 1: Explicit museum metadata ───────────────────
        self._examine_museum_metadata(artwork, evidence, raw_values)

        # ── Priority 2: Known artist mappings ──────────────────────
        self._examine_artist(artwork, evidence, raw_values)

        # ── Priority 3: Date range heuristics ──────────────────────
        self._examine_date_range(artwork, evidence)

        # ── Aggregate evidence into classification ─────────────────
        return self._aggregate(artwork, evidence, raw_values)

    def _examine_museum_metadata(
        self,
        artwork: Artwork,
        evidence: list[ClassificationEvidence],
        raw_values: list[str],
    ) -> None:
        """Priority 1: Check explicit museum-provided metadata fields."""
        # Check movement_raw
        if artwork.movement_raw:
            raw_values.append(artwork.movement_raw)
            movement = normalize_movement(artwork.movement_raw)
            if movement:
                evidence.append(
                    ClassificationEvidence(
                        evidence_type=EvidenceType.MUSEUM_MOVEMENT_FIELD,
                        raw_value=artwork.movement_raw,
                        normalized_to=movement,
                        weight=1.0,
                        note="Direct museum movement metadata.",
                    )
                )

        # Check style_raw
        if artwork.style_raw:
            raw_values.append(artwork.style_raw)
            movement = normalize_movement(artwork.style_raw)
            if movement:
                evidence.append(
                    ClassificationEvidence(
                        evidence_type=EvidenceType.MUSEUM_STYLE_FIELD,
                        raw_value=artwork.style_raw,
                        normalized_to=movement,
                        weight=0.9,
                        note="Museum style metadata.",
                    )
                )

        # Check period_raw
        if artwork.period_raw:
            raw_values.append(artwork.period_raw)
            movement = normalize_movement(artwork.period_raw)
            if movement:
                evidence.append(
                    ClassificationEvidence(
                        evidence_type=EvidenceType.MUSEUM_PERIOD_FIELD,
                        raw_value=artwork.period_raw,
                        normalized_to=movement,
                        weight=0.7,
                        note="Museum period metadata.",
                    )
                )

        # Check classification field
        if artwork.classification:
            movement = normalize_movement(artwork.classification)
            if movement:
                raw_values.append(artwork.classification)
                evidence.append(
                    ClassificationEvidence(
                        evidence_type=EvidenceType.MUSEUM_CLASSIFICATION_FIELD,
                        raw_value=artwork.classification,
                        normalized_to=movement,
                        weight=0.6,
                    )
                )

        # Check tags
        for tag in artwork.tags:
            movement = normalize_movement(tag)
            if movement:
                raw_values.append(tag)
                evidence.append(
                    ClassificationEvidence(
                        evidence_type=EvidenceType.MUSEUM_TAGS,
                        raw_value=tag,
                        normalized_to=movement,
                        weight=0.5,
                        note=f"Matched tag: {tag}",
                    )
                )

    def _examine_artist(
        self,
        artwork: Artwork,
        evidence: list[ClassificationEvidence],
        raw_values: list[str],
    ) -> None:
        """Priority 2: Check known canonical artist → movement mappings."""
        if not artwork.artist_name:
            return

        movements = lookup_artist_movements(artwork.artist_name)
        if movements:
            raw_values.append(f"artist:{artwork.artist_name}")
            for i, movement in enumerate(movements):
                # Primary artist movement gets higher weight
                weight = 0.75 if i == 0 else 0.4
                evidence.append(
                    ClassificationEvidence(
                        evidence_type=EvidenceType.CANONICAL_ARTIST_MAPPING,
                        raw_value=artwork.artist_name,
                        normalized_to=movement,
                        weight=weight,
                        note=(
                            f"Known {'primary' if i == 0 else 'secondary'}"
                            f" movement for {artwork.artist_name}."
                        ),
                    )
                )

    def _examine_date_range(
        self,
        artwork: Artwork,
        evidence: list[ClassificationEvidence],
    ) -> None:
        """Priority 3: Date-based heuristic (low weight, never definitive).

        Date alone is NOT proof of movement. This provides only a weak hint.
        """
        if artwork.year_start is None:
            return

        year = artwork.year_start
        movement = self._date_to_broad_movement(year)
        if movement and movement != ArtMovement.UNCLASSIFIED:
            evidence.append(
                ClassificationEvidence(
                    evidence_type=EvidenceType.DATE_RANGE_HEURISTIC,
                    raw_value=str(year),
                    normalized_to=movement,
                    weight=0.2,  # Very low — date alone is weak evidence
                    note=f"Date-based heuristic for year {year}. Weak signal only.",
                )
            )

    def _date_to_broad_movement(self, year: int) -> ArtMovement | None:
        """Map a year to a very broad movement hint.

        This is intentionally coarse. It exists only to break ties
        when other evidence is ambiguous.
        """
        if year < 500:
            return ArtMovement.ANCIENT
        if year < 1400:
            return ArtMovement.MEDIEVAL
        if year < 1500:
            return ArtMovement.RENAISSANCE
        if year < 1600:
            return ArtMovement.RENAISSANCE
        if year < 1720:
            return ArtMovement.BAROQUE
        if year < 1790:
            return ArtMovement.ROCOCO
        if year < 1850:
            return ArtMovement.ROMANTICISM
        if year < 1875:
            return ArtMovement.REALISM
        if year < 1910:
            return ArtMovement.IMPRESSIONISM
        if year < 1940:
            return ArtMovement.MODERNISM
        if year < 1970:
            return ArtMovement.MODERNISM
        return ArtMovement.CONTEMPORARY

    def _aggregate(
        self,
        artwork: Artwork,
        evidence: list[ClassificationEvidence],
        raw_values: list[str],
    ) -> MovementClassification:
        """Aggregate evidence into a final classification.

        Strategy:
          - Weight-sum each movement's evidence.
          - Primary = movement with highest weighted score.
          - Confidence = normalized weighted score.
          - Confidence level = binned by thresholds.
        """
        if not evidence:
            logger.info(
                "classification_unclassified",
                artwork_id=artwork.canonical_id,
                reason="no_evidence",
            )
            return MovementClassification.unclassified(raw_values)

        # Compute weighted scores per movement
        scores: dict[ArtMovement, float] = defaultdict(float)
        for ev in evidence:
            if ev.normalized_to != ArtMovement.UNCLASSIFIED:
                scores[ev.normalized_to] += ev.weight

        if not scores:
            return MovementClassification.unclassified(raw_values)

        # Primary = highest score
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        primary, primary_score = sorted_scores[0]

        # Secondary = other movements with significant evidence
        secondary = [m for m, s in sorted_scores if m != primary and s >= self._minimum_threshold]

        # Normalize confidence to 0.0-1.0
        max_possible = sum(ev.weight for ev in evidence)
        confidence = min(primary_score / max(max_possible, 1.0), 1.0)

        # Determine confidence level
        primary_evidence_types = {
            ev.evidence_type for ev in evidence if ev.normalized_to == primary
        }
        is_date_only = primary_evidence_types == {EvidenceType.DATE_RANGE_HEURISTIC}

        if is_date_only:
            # INVARIANT: Date range heuristics alone can NEVER produce HIGH or MEDIUM confidence.
            # They provide only a weak temporal hint, never sufficient for auto-routing.
            confidence = min(confidence, 0.35)
            level = (
                ConfidenceLevel.LOW
                if confidence >= self._minimum_threshold
                else ConfidenceLevel.NONE
            )
        elif confidence >= self._auto_threshold:
            level = ConfidenceLevel.HIGH
        elif confidence >= MEDIUM_CONFIDENCE_THRESHOLD:
            level = ConfidenceLevel.MEDIUM
        elif confidence >= self._minimum_threshold:
            level = ConfidenceLevel.LOW
        else:
            level = ConfidenceLevel.NONE

        # Build reason string
        evidence_sources = ", ".join(
            sorted(ev.evidence_type.value for ev in evidence if ev.normalized_to == primary)
        )
        reason = f"Classified as {primary.value} based on: {evidence_sources}."

        result = MovementClassification(
            primary_movement=primary,
            secondary_movements=secondary,
            confidence=round(confidence, 3),
            confidence_level=level,
            evidence=evidence,
            raw_values=raw_values,
            reason=reason,
        )

        logger.info(
            "classification_complete",
            artwork_id=artwork.canonical_id,
            movement=primary.value,
            confidence=result.confidence,
            confidence_level=level.value,
            evidence_count=len(evidence),
        )

        return result
