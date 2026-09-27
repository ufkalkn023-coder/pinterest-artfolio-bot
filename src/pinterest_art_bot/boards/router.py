"""Board router — deterministic movement → board routing.

This is a first-class subsystem, not an afterthought.
The router consults board configuration to map a MovementClassification
to a specific BoardTarget.

Key invariants:
  - UNCLASSIFIED artworks must NOT be silently routed to any board.
  - Low-confidence classifications must NOT auto-route.
  - Routing is deterministic for a given classification + config.
  - Board IDs are never hardcoded here — they come from config.
"""

from __future__ import annotations

from enum import StrEnum

import structlog

from pinterest_art_bot.boards.config import BoardsConfiguration, default_boards_config
from pinterest_art_bot.domain.movement import ArtMovement, ConfidenceLevel, MovementClassification
from pinterest_art_bot.domain.publication import BoardTarget

logger = structlog.get_logger()


class RoutingStatus(StrEnum):
    """Explicit status for routing decision."""

    ROUTED = "routed"
    ROUTING_HELD = "routing_held"
    NO_BOARD = "no_board"
    DISABLED = "disabled"


class RoutingResult:
    """Result of a board routing attempt."""

    def __init__(
        self,
        target: BoardTarget | None,
        routed: bool,
        reason: str,
        movement: ArtMovement,
        confidence_level: ConfidenceLevel,
        status: RoutingStatus = RoutingStatus.ROUTED,
        is_held: bool = False,
    ) -> None:
        self.target = target
        self.routed = routed
        self.reason = reason
        self.movement = movement
        self.confidence_level = confidence_level
        self.status = status
        self.is_held = is_held

    def __repr__(self) -> str:
        if self.routed and self.target:
            return f"RoutingResult(→ {self.target.board_name}, {self.movement.value})"
        return f"RoutingResult(status={self.status.value}, reason={self.reason})"


class BoardRouter:
    """Deterministic board router.

    Maps a MovementClassification to a BoardTarget using the
    configured movement → board mappings.
    """

    def __init__(
        self,
        config: BoardsConfiguration | None = None,
        *,
        allow_medium_confidence: bool = False,
    ) -> None:
        self._config = config or default_boards_config()
        self._allow_medium = allow_medium_confidence

    def route(self, classification: MovementClassification) -> RoutingResult:
        """Route an artwork to a board based on its classification.

        Returns a RoutingResult indicating success/failure and reason.
        """
        movement = classification.primary_movement

        # ── Gate: UNCLASSIFIED cannot route ─────────────────────────
        if movement == ArtMovement.UNCLASSIFIED:
            logger.info("routing_skipped", reason="unclassified")
            return RoutingResult(
                target=None,
                routed=False,
                status=RoutingStatus.ROUTING_HELD,
                is_held=True,
                reason="Artwork is unclassified. Routing held for review.",
                movement=movement,
                confidence_level=classification.confidence_level,
            )

        # ── Gate: Low confidence cannot auto-route ──────────────────
        if classification.confidence_level == ConfidenceLevel.LOW:
            logger.info(
                "routing_skipped",
                reason="low_confidence",
                movement=movement.value,
                confidence=classification.confidence,
            )
            return RoutingResult(
                target=None,
                routed=False,
                status=RoutingStatus.ROUTING_HELD,
                is_held=True,
                reason=(
                    f"Confidence too low ({classification.confidence:.2f})"
                    f" for movement {movement.value}. Routing held for review."
                ),
                movement=movement,
                confidence_level=classification.confidence_level,
            )

        if classification.confidence_level == ConfidenceLevel.NONE:
            return RoutingResult(
                target=None,
                routed=False,
                status=RoutingStatus.ROUTING_HELD,
                is_held=True,
                reason="No classification confidence. Routing held for review.",
                movement=movement,
                confidence_level=classification.confidence_level,
            )

        # ── Gate: MEDIUM confidence requires explicit opt-in ────────
        if classification.confidence_level == ConfidenceLevel.MEDIUM and not self._allow_medium:
            logger.info(
                "routing_skipped",
                reason="medium_confidence_not_allowed",
                movement=movement.value,
            )
            return RoutingResult(
                target=None,
                routed=False,
                status=RoutingStatus.ROUTING_HELD,
                is_held=True,
                reason=(
                    f"Medium confidence ({classification.confidence:.2f})"
                    f" for {movement.value}."
                    " Auto-routing not enabled for medium confidence. Routing held for review."
                ),
                movement=movement,
                confidence_level=classification.confidence_level,
            )

        # ── Look up board for movement ──────────────────────────────
        board_config = self._config.get_board_for_movement(movement)
        if board_config is None:
            logger.warning(
                "routing_no_board",
                movement=movement.value,
            )
            return RoutingResult(
                target=None,
                routed=False,
                status=RoutingStatus.NO_BOARD,
                is_held=False,
                reason=f"No board configured for movement: {movement.value}.",
                movement=movement,
                confidence_level=classification.confidence_level,
            )

        if not board_config.enabled:
            return RoutingResult(
                target=None,
                routed=False,
                status=RoutingStatus.DISABLED,
                is_held=False,
                reason=f"Board '{board_config.board_name}' is disabled.",
                movement=movement,
                confidence_level=classification.confidence_level,
            )

        target = self._config.to_board_target(board_config)

        logger.info(
            "routing_success",
            movement=movement.value,
            board_key=target.board_key,
            board_name=target.board_name,
            confidence=classification.confidence,
        )

        return RoutingResult(
            target=target,
            routed=True,
            status=RoutingStatus.ROUTED,
            is_held=False,
            reason=f"Routed {movement.value} to '{target.board_name}'.",
            movement=movement,
            confidence_level=classification.confidence_level,
        )
