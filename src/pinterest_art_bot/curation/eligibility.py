"""Curation eligibility evaluator.

Determines whether an artwork that has passed rights checks and
classification is actually suitable for publication as a Pin.

This is NOT the rights gate (that's in rights/evaluator.py).
This layer handles quality, completeness, and diversity signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.movement import ConfidenceLevel, MovementClassification
from pinterest_art_bot.domain.rights import RightsEvaluation
from pinterest_art_bot.media.validation import MediaValidationResult, MediaValidator


@dataclass
class CurationDecision:
    """Result of curation evaluation."""

    eligible: bool = True
    reasons: list[str] = field(default_factory=list)
    media_validation: MediaValidationResult | None = None

    def reject(self, reason: str) -> None:
        self.eligible = False
        self.reasons.append(reason)


class EligibilityEvaluator:
    """Evaluates whether an artwork should proceed to publication.

    Checks:
      - Rights approval
      - Movement classification confidence
      - Image availability and quality
      - Metadata completeness
    """

    def __init__(
        self,
        media_validator: MediaValidator | None = None,
        *,
        require_high_confidence: bool = True,
    ) -> None:
        self._media_validator = media_validator or MediaValidator()
        self._require_high = require_high_confidence

    def evaluate(
        self,
        artwork: Artwork,
        rights: RightsEvaluation,
        classification: MovementClassification,
    ) -> CurationDecision:
        """Evaluate an artwork for publication eligibility."""
        decision = CurationDecision()

        # ── Rights must be approved ─────────────────────────────────
        if not rights.is_publishable:
            decision.reject(f"Rights not approved: {rights.reason}")

        # ── Must be classified ──────────────────────────────────────
        if not classification.is_classified:
            decision.reject("Artwork is unclassified.")

        # ── Confidence check ────────────────────────────────────────
        if self._require_high and classification.confidence_level != ConfidenceLevel.HIGH:
            decision.reject(
                f"Classification confidence {classification.confidence_level.value} "
                f"is below required HIGH."
            )
        elif classification.confidence_level in (ConfidenceLevel.LOW, ConfidenceLevel.NONE):
            decision.reject(
                f"Classification confidence too low: {classification.confidence_level.value}."
            )

        # ── Must have an image ──────────────────────────────────────
        if artwork.best_image_url is None:
            decision.reject("No image available.")
        else:
            # Validate image metadata
            media_result = self._media_validator.validate_metadata(
                width=artwork.image_width,
                height=artwork.image_height,
                url=artwork.best_image_url,
            )
            decision.media_validation = media_result
            if not media_result.is_valid:
                for issue in media_result.issues:
                    decision.reject(f"Media: {issue}")

        # ── Must have a title ───────────────────────────────────────
        if not artwork.title or artwork.title.strip() in ("", "Untitled"):
            # Untitled is technically valid but less desirable
            pass  # Don't reject, but could add a quality score penalty later

        return decision
