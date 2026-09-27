"""Tests for board routing.

Core invariants:
  - UNCLASSIFIED must NOT route to any board.
  - Low confidence must NOT auto-route.
  - Every canonical movement should have a configured board.
"""

from pinterest_art_bot.boards.config import default_boards_config
from pinterest_art_bot.boards.router import BoardRouter, RoutingStatus
from pinterest_art_bot.domain.movement import (
    ArtMovement,
    ClassificationEvidence,
    ConfidenceLevel,
    EvidenceType,
    MovementClassification,
)


def _make_classification(
    movement: ArtMovement,
    confidence: float = 0.95,
    level: ConfidenceLevel = ConfidenceLevel.HIGH,
) -> MovementClassification:
    """Helper to create a classification for routing tests."""
    return MovementClassification(
        primary_movement=movement,
        confidence=confidence,
        confidence_level=level,
        evidence=[
            ClassificationEvidence(
                evidence_type=EvidenceType.MUSEUM_STYLE_FIELD,
                raw_value=movement.value,
                normalized_to=movement,
                weight=1.0,
            )
        ],
        raw_values=[movement.value],
        reason=f"Test classification: {movement.value}",
    )


class TestBoardRouter:
    """Board routing must be deterministic and safe."""

    def setup_method(self):
        self.router = BoardRouter()

    def test_baroque_routes_to_baroque_board(self):
        result = self.router.route(_make_classification(ArtMovement.BAROQUE))
        assert result.routed is True
        assert result.target is not None
        assert result.target.board_name == "Baroque Art"
        assert result.target.board_key == "baroque"

    def test_impressionism_routes_to_impressionism_board(self):
        result = self.router.route(_make_classification(ArtMovement.IMPRESSIONISM))
        assert result.routed is True
        assert result.target is not None
        assert result.target.board_name == "Impressionism"

    def test_post_impressionism_routes(self):
        result = self.router.route(_make_classification(ArtMovement.POST_IMPRESSIONISM))
        assert result.routed is True
        assert result.target is not None
        assert result.target.board_name == "Post-Impressionism"

    def test_renaissance_routes(self):
        result = self.router.route(_make_classification(ArtMovement.RENAISSANCE))
        assert result.routed is True
        assert result.target is not None
        assert result.target.board_name == "Renaissance Art"

    def test_surrealism_routes(self):
        result = self.router.route(_make_classification(ArtMovement.SURREALISM))
        assert result.routed is True
        assert result.target is not None
        assert result.target.board_name == "Surrealism"

    def test_unclassified_does_not_route(self):
        """CRITICAL: UNCLASSIFIED must NOT silently route to any board."""
        result = self.router.route(MovementClassification.unclassified())
        assert result.routed is False
        assert result.target is None
        assert result.status == RoutingStatus.ROUTING_HELD
        assert result.is_held is True
        assert "unclassified" in result.reason.lower()

    def test_low_confidence_does_not_route(self):
        """Low confidence must NOT auto-route — must be HELD."""
        classification = _make_classification(
            ArtMovement.BAROQUE,
            confidence=0.2,
            level=ConfidenceLevel.LOW,
        )
        result = self.router.route(classification)
        assert result.routed is False
        assert result.target is None
        assert result.status == RoutingStatus.ROUTING_HELD
        assert result.is_held is True

    def test_medium_confidence_default_does_not_route(self):
        """Medium confidence must not route by default — must be HELD."""
        classification = _make_classification(
            ArtMovement.BAROQUE,
            confidence=0.6,
            level=ConfidenceLevel.MEDIUM,
        )
        result = self.router.route(classification)
        assert result.routed is False
        assert result.status == RoutingStatus.ROUTING_HELD
        assert result.is_held is True

    def test_medium_confidence_routes_when_allowed(self):
        """Medium confidence can route when explicitly enabled."""
        router = BoardRouter(allow_medium_confidence=True)
        classification = _make_classification(
            ArtMovement.BAROQUE,
            confidence=0.6,
            level=ConfidenceLevel.MEDIUM,
        )
        result = router.route(classification)
        assert result.routed is True
        assert result.status == RoutingStatus.ROUTED
        assert result.is_held is False

    def test_all_non_unclassified_movements_have_boards(self):
        """Every canonical movement (except UNCLASSIFIED) should have a board."""
        config = default_boards_config()
        unmapped = []
        for movement in ArtMovement:
            if movement == ArtMovement.UNCLASSIFIED:
                continue
            board = config.get_board_for_movement(movement)
            if board is None:
                unmapped.append(movement.value)
        assert unmapped == [], f"Movements without boards: {unmapped}"

    def test_routing_result_includes_movement(self):
        result = self.router.route(_make_classification(ArtMovement.BAROQUE))
        assert result.movement == ArtMovement.BAROQUE

    def test_routing_result_includes_confidence_level(self):
        result = self.router.route(_make_classification(ArtMovement.BAROQUE))
        assert result.confidence_level == ConfidenceLevel.HIGH
