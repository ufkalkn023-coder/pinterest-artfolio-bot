"""Pin text configuration.

Templates and keywords are configuration, not code: they decide how pins read in
Pinterest search, which changes far more often than the publishing logic.

Template resolution order: board template → movement template → default.
Unknown placeholders are dropped, so a typo degrades the copy instead of
breaking a publish run.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, Field

from pinterest_art_bot.domain.movement import ArtMovement
from pinterest_art_bot.domain.normalization import normalize_person_name

logger = structlog.get_logger()


class PinTemplate(BaseModel):
    """Title and description templates for a scope."""

    title: str | None = None
    description: str | None = None


class PinTextConfig(BaseModel):
    """Template configuration, loaded from YAML."""

    default: PinTemplate = Field(default_factory=PinTemplate)
    boards: dict[str, PinTemplate] = Field(default_factory=dict)
    movements: dict[str, PinTemplate] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)

    def resolve(self, board_key: str | None, movement: ArtMovement | None) -> PinTemplate:
        """Pick the most specific template available for a pin."""
        return (
            self.boards.get(board_key or "")
            or self.movements.get(movement.value if movement else "")
            or self.default
        )


def load_pin_templates(path: Path | None) -> PinTextConfig:
    """Load pin text configuration, falling back to sensible built-in copy."""
    if path and path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return PinTextConfig.model_validate(data)
        except Exception as e:
            logger.warning("pin_templates_unreadable", path=str(path), error=str(e))
    return PinTextConfig(
        default=PinTemplate(
            title="{title} — {artist_display}",
            description="{artist_display} · {movement_display}\n{date}{medium_line}\n"
            "From the collection of {collection}.\n\n"
            "More {movement_display} art on this board.",
        ),
        keywords=["art", "arthistory", "painting", "museum"],
    )


@dataclass(frozen=True)
class PinTextContext:
    """Every value a template may reference."""

    title: str
    artist: str | None
    date: str | None
    medium: str | None
    movement_display: str
    movement_keyword: str
    collection: str
    department: str | None
    culture: str | None
    board: str
    source: str

    @property
    def display_artist(self) -> str:
        """Artist name without museum decoration, for public copy.

        "Claude Lorrain (Claude Gellée)" reads as "Claude Lorrain"; museum
        annotations are noise for readers and for Pinterest search.
        """
        if not self.artist:
            return "Unknown Artist"
        cleaned = normalize_person_name(self.artist)
        if not cleaned:
            return "Unknown Artist"
        # Restore the museum's own capitalisation.
        return " ".join(word.capitalize() if word.islower() else word for word in cleaned.split())

    def as_mapping(self) -> dict[str, str]:
        """Placeholder values, with every optional field coerced to text."""
        artist = self.display_artist
        return {
            "title": self.title,
            "artist": self.artist or "",
            "artist_display": artist,
            "year": self.date or "",
            "date": self.date or "",
            "medium": self.medium or "",
            "medium_line": f"\n{self.medium}\n" if self.medium else "\n",
            "movement": self.movement_display.lower().replace(" ", "_"),
            "movement_display": self.movement_display,
            "movement_keyword": self.movement_keyword,
            "collection": self.collection,
            "department": self.department or "",
            "culture": self.culture or "",
            "board": self.board,
            "source": self.source,
        }


class _SafeFormatter(string.Formatter):
    """Formatter that leaves unknown placeholders empty instead of raising."""

    def get_value(self, key: str | int | Any, args: Any, kwargs: Any) -> Any:
        if isinstance(key, str) and key not in kwargs:
            logger.debug("pin_template_unknown_placeholder", placeholder=key)
            return ""
        return super().get_value(key, args, kwargs)


_FORMATTER = _SafeFormatter()


def render(template: str | None, context: PinTextContext) -> str:
    """Render a template, collapsing the blank lines that result."""
    if not template:
        return ""
    try:
        rendered = _FORMATTER.vformat(template, (), context.as_mapping())
    except Exception as e:
        logger.warning("pin_template_render_failed", error=str(e))
        return ""
    return _tidy(rendered)


def _tidy(text: str) -> str:
    """Collapse repeated blank lines and strip outer whitespace."""
    lines = [" ".join(line.split()) if line.strip() else "" for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


def keyword_hashtags(
    context: PinTextContext,
    configured: list[str],
    *,
    limit: int = 8,
) -> str:
    """Build the hashtag line from template keywords plus configured ones."""
    terms: list[str] = []

    def add(term: str) -> None:
        cleaned = "".join(char for char in (term or "").lower() if char.isalnum())
        if cleaned and cleaned not in terms:
            terms.append(cleaned)

    add(context.movement_keyword)
    if context.department:
        # Long department names ("Modern European Painting and Sculpture") make
        # useless hashtags; only short, distinctive ones are worth a tag.
        add(context.department if len(context.department) <= 20 else "")
    for term in configured:
        add(term)

    return " ".join(f"#{term}" for term in terms[:limit])


@dataclass
class TemplateRenderer:
    """Renders titles, descriptions, and hashtags from configuration."""

    config: PinTextConfig
    board_key: str | None = None
    movement: ArtMovement | None = None
    extra_keywords: list[str] = field(default_factory=list)

    def render_title(self, context: PinTextContext) -> str:
        """Render the title, falling back to a plain work-title-plus-artist form."""
        template = self.config.resolve(self.board_key, self.movement)
        rendered = render(template.title, context)
        return rendered or f"{context.title} — {context.artist or 'Unknown Artist'}"

    def render_description(self, context: PinTextContext) -> str:
        """Render the description body (licence lines are added by the composer)."""
        template = self.config.resolve(self.board_key, self.movement)
        body = render(template.description, context)
        hashtags = keyword_hashtags(context, [*self.config.keywords, *self.extra_keywords])
        if hashtags:
            body = f"{body}\n\n{hashtags}" if body else hashtags
        return body
