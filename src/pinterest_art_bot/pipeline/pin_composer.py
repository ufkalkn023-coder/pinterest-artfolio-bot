"""Pin metadata composition.

Single place where Pinterest-facing text is produced. Keeping this out of the
pipeline runner means the copy, the length limits, and the licence disclosure
rules can be tested and changed independently of orchestration.

Compliance rules enforced here:
  - Every length limit comes from settings, never from a constant.
  - When rights require attribution, the attribution text AND the canonical
    licence URL are always present in the description.
"""

from __future__ import annotations

from pathlib import Path

from pinterest_art_bot.domain.artwork import Artwork
from pinterest_art_bot.domain.publication import BoardTarget, PinMetadata
from pinterest_art_bot.domain.rights import RightsEvaluation, RightsStatus
from pinterest_art_bot.pipeline.pin_templates import (
    PinTextConfig,
    PinTextContext,
    TemplateRenderer,
    load_pin_templates,
)

DEFAULT_PIN_TITLE_MAX_LENGTH = 100
DEFAULT_PIN_DESCRIPTION_MAX_LENGTH = 800
DEFAULT_PIN_ALT_TEXT_MAX_LENGTH = 500

# Canonical licence URLs, so a Pin always states the actual licence.
LICENSE_URLS: dict[RightsStatus, str] = {
    RightsStatus.PUBLIC_DOMAIN: "",
    RightsStatus.OPEN_ACCESS: "",
    RightsStatus.CC0: "https://creativecommons.org/publicdomain/zero/1.0/",
    RightsStatus.CC_BY: "https://creativecommons.org/licenses/by/4.0/",
    RightsStatus.CC_BY_SA: "https://creativecommons.org/licenses/by-sa/4.0/",
    RightsStatus.ATTRIBUTION_REQUIRED: "",
}

LICENSE_LABELS: dict[RightsStatus, str] = {
    RightsStatus.PUBLIC_DOMAIN: "Public domain",
    RightsStatus.OPEN_ACCESS: "Open access",
    RightsStatus.CC0: "CC0 1.0 (public domain dedication)",
    RightsStatus.CC_BY: "CC BY 4.0 (attribution required)",
    RightsStatus.CC_BY_SA: "CC BY-SA 4.0 (attribution and share-alike required)",
    RightsStatus.ATTRIBUTION_REQUIRED: "Attribution required",
}

# Characters Pinterest rejects or renders badly in Pin text.
_ILLEGAL_CHARS = str.maketrans({"\x00": " ", "\r": " ", "\t": " "})


def truncate(value: str, limit: int) -> str:
    """Trim to a hard limit, marking truncation explicitly."""
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def sanitize(value: str) -> str:
    """Strip control characters and collapse runaway whitespace."""
    cleaned = value.translate(_ILLEGAL_CHARS)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


class PinComposer:
    """Builds Pinterest-ready metadata from an artwork and its evaluation."""

    def __init__(
        self,
        *,
        title_max_length: int = DEFAULT_PIN_TITLE_MAX_LENGTH,
        description_max_length: int = DEFAULT_PIN_DESCRIPTION_MAX_LENGTH,
        alt_text_max_length: int = DEFAULT_PIN_ALT_TEXT_MAX_LENGTH,
        text_config: PinTextConfig | None = None,
        extra_keywords: list[str] | None = None,
    ) -> None:
        self._title_limit = title_max_length
        self._description_limit = description_max_length
        self._alt_limit = alt_text_max_length
        # No configuration means the built-in copy, not empty copy.
        self._text_config = text_config or load_pin_templates(None)
        self._extra_keywords = extra_keywords or []

    @property
    def limits(self) -> dict[str, int]:
        """Active length limits, for diagnostics and tests."""
        return {
            "title": self._title_limit,
            "description": self._description_limit,
            "alt_text": self._alt_limit,
        }

    def compose(
        self,
        artwork: Artwork,
        movement_name: str,
        rights: RightsEvaluation,
        board_target: BoardTarget,
    ) -> PinMetadata:
        """Build the Pin metadata for a fully gated artwork."""
        return PinMetadata(
            title=self.build_title(artwork, movement_name, board_target),
            description=self.build_description(artwork, movement_name, rights, board_target),
            link=artwork.museum_object_url,
            alt_text=self.build_alt_text(artwork),
            board_target=board_target,
        )

    def build_context(
        self,
        artwork: Artwork,
        movement_name: str,
        board_target: BoardTarget | None = None,
    ) -> PinTextContext:
        """Assemble the values every template may reference."""
        movement_display = movement_name.replace("_", " ").title()
        return PinTextContext(
            title=artwork.title,
            artist=artwork.artist_name,
            date=artwork.creation_date_text or _year_label(artwork),
            medium=artwork.medium,
            movement_display=movement_display,
            movement_keyword=movement_display.replace(" ", "").lower(),
            collection=artwork.museum_name,
            department=artwork.department,
            culture=artwork.culture,
            board=board_target.board_name if board_target else "",
            source=artwork.identity.source,
        )

    def _renderer(self, movement_name: str, board_target: BoardTarget | None) -> TemplateRenderer:
        movement = None
        try:
            from pinterest_art_bot.domain.movement import ArtMovement

            movement = ArtMovement(movement_name)
        except ValueError:
            movement = None
        return TemplateRenderer(
            config=self._text_config,
            board_key=board_target.board_key if board_target else None,
            movement=movement,
            extra_keywords=self._extra_keywords,
        )

    def build_title(
        self,
        artwork: Artwork,
        movement_name: str = "art",
        board_target: BoardTarget | None = None,
    ) -> str:
        """Pin title from the configured template, within the length limit."""
        context = self.build_context(artwork, movement_name, board_target)
        rendered = self._renderer(movement_name, board_target).render_title(context)
        if not rendered:
            rendered = f"{context.title} — {context.display_artist}"
        return truncate(sanitize(rendered), self._title_limit)

    def build_alt_text(self, artwork: Artwork) -> str:
        """Alt text describing the image for accessibility.

        Uses the same normalized artist name as the visible copy, so museum
        annotations do not leak into public text.
        """
        context = self.build_context(artwork, "art")
        subject = context.display_artist if artwork.artist_name else "classical art"
        medium = f", {artwork.medium}" if artwork.medium else ""
        return truncate(sanitize(f"{artwork.title} by {subject}{medium}"), self._alt_limit)

    def build_description(
        self,
        artwork: Artwork,
        movement_name: str,
        rights: RightsEvaluation,
        board_target: BoardTarget | None = None,
    ) -> str:
        """Build the Pin description: templated copy plus licence disclosure.

        The licence block is always appended, whatever the template says, so a
        copy change can never drop a disclosure.
        """
        context = self.build_context(artwork, movement_name, board_target)
        body = self._renderer(movement_name, board_target).render_description(context)

        lines: list[str] = [body] if body else []
        lines.extend(self._license_lines(artwork, rights))

        if rights.requires_attribution:
            attribution = rights.attribution_text or artwork.credit_line
            if attribution:
                lines.append(f"Credit: {attribution}")
            else:
                # Defensive: the rights gate should already have denied this,
                # but a Pin must never imply unattributed reuse.
                lines.append("Credit: not stated by the source museum")

        return truncate(sanitize("\n".join(lines)), self._description_limit)

    def _license_lines(
        self,
        artwork: Artwork,
        rights: RightsEvaluation,
    ) -> list[str]:
        """Licence disclosure lines for the description."""
        label = LICENSE_LABELS.get(rights.status, rights.status.value)
        url = LICENSE_URLS.get(rights.status, "")

        if rights.requires_attribution:
            return [f"License: {label} ({url})"] if url else [f"License: {label}"]

        if url:
            return [f"License: {label} ({url})"]

        # Public domain / open access: state the source so reuse is traceable.
        if artwork.credit_line:
            return [f"License: {label}", f"Credit: {artwork.credit_line}"]
        return [f"License: {label}"]


def _year_label(artwork: Artwork) -> str | None:
    """Fallback date when the museum gives no display string."""
    if artwork.year_start and artwork.year_end and artwork.year_start != artwork.year_end:
        return f"{artwork.year_start}-{artwork.year_end}"
    if artwork.year_start:
        return str(artwork.year_start)
    return None


def default_pin_composer(templates_path: Path | None = None) -> PinComposer:
    """Composer with platform default limits and loaded templates."""
    return PinComposer(text_config=load_pin_templates(templates_path))


__all__ = [
    "DEFAULT_PIN_ALT_TEXT_MAX_LENGTH",
    "DEFAULT_PIN_DESCRIPTION_MAX_LENGTH",
    "DEFAULT_PIN_TITLE_MAX_LENGTH",
    "LICENSE_LABELS",
    "LICENSE_URLS",
    "PinComposer",
    "default_pin_composer",
    "sanitize",
    "truncate",
]
