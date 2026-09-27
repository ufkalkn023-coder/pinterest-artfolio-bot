"""Tests for curation eligibility evaluation."""

from pinterest_art_bot.curation.eligibility import EligibilityEvaluator
from pinterest_art_bot.domain.artwork import Artwork, ArtworkIdentity
from pinterest_art_bot.domain.movement import (
    ArtMovement,
    ClassificationEvidence,
    ConfidenceLevel,
    EvidenceType,
    MovementClassification,
)
from pinterest_art_bot.domain.rights import RightsEvaluation, RightsStatus


def _approved_rights(artwork_id: str) -> RightsEvaluation:
    return RightsEvaluation(
        artwork_id=artwork_id,
        status=RightsStatus.PUBLIC_DOMAIN,
        is_publishable=True,
        reason="Public domain.",
    )


def _denied_rights(artwork_id: str) -> RightsEvaluation:
    return RightsEvaluation.deny(artwork_id)


def _high_classification(movement: ArtMovement = ArtMovement.BAROQUE) -> MovementClassification:
    return MovementClassification(
        primary_movement=movement,
        confidence=0.95,
        confidence_level=ConfidenceLevel.HIGH,
        evidence=[
            ClassificationEvidence(
                evidence_type=EvidenceType.MUSEUM_STYLE_FIELD,
                raw_value=movement.value,
                normalized_to=movement,
            )
        ],
    )


class TestEligibilityEvaluator:
    """Curation must check rights, classification, and image quality."""

    def setup_method(self):
        self.evaluator = EligibilityEvaluator()

    def test_eligible_artwork(self, sample_artwork: Artwork):
        rights = _approved_rights(sample_artwork.canonical_id)
        classification = _high_classification(ArtMovement.POST_IMPRESSIONISM)
        decision = self.evaluator.evaluate(sample_artwork, rights, classification)
        assert decision.eligible is True

    def test_denied_rights_not_eligible(self, sample_artwork: Artwork):
        rights = _denied_rights(sample_artwork.canonical_id)
        classification = _high_classification()
        decision = self.evaluator.evaluate(sample_artwork, rights, classification)
        assert decision.eligible is False
        assert any("rights" in r.lower() for r in decision.reasons)

    def test_unclassified_not_eligible(self, sample_artwork: Artwork):
        rights = _approved_rights(sample_artwork.canonical_id)
        classification = MovementClassification.unclassified()
        decision = self.evaluator.evaluate(sample_artwork, rights, classification)
        assert decision.eligible is False

    def test_no_image_not_eligible(self):
        artwork = Artwork(
            identity=ArtworkIdentity(source="test", source_object_id="1"),
            title="No Image",
            museum_name="Test Museum",
        )
        rights = _approved_rights("test:1")
        classification = _high_classification()
        decision = self.evaluator.evaluate(artwork, rights, classification)
        assert decision.eligible is False
        assert any("image" in r.lower() for r in decision.reasons)
