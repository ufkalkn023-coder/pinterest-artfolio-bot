"""Pinterest API response models.

Separate from domain models — these represent Pinterest's API responses,
not our internal representations.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PinterestBoard(BaseModel):
    """Pinterest board as returned by the API."""

    id: str
    name: str
    description: str | None = None
    privacy: str = "PUBLIC"
    pin_count: int | None = None


class PinterestPin(BaseModel):
    """Pinterest pin as returned by the API."""

    id: str
    board_id: str
    title: str | None = None
    description: str | None = None
    link: str | None = None
    media_url: str | None = None
    alt_text: str | None = None
    created_at: str | None = None
    url: str | None = Field(
        default=None,
        description="Public Pin URL, e.g. 'https://www.pinterest.com/pin/123/'.",
    )


class CreatePinRequest(BaseModel):
    """Request to create a new Pin."""

    board_id: str
    title: str
    description: str
    link: str | None = None
    alt_text: str | None = None
    media_source_url: str | None = None
    media_base64: str | None = None


class CreateBoardRequest(BaseModel):
    """Request to create a new board."""

    name: str
    description: str | None = None
    privacy: str = "PUBLIC"
