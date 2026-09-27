"""Pinterest API errors."""

from __future__ import annotations


class PinterestError(Exception):
    """Base class for Pinterest API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PinterestAuthError(PinterestError):
    """Authentication/authorization failure."""


class PinterestRateLimitError(PinterestError):
    """Rate limit exceeded."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class PinterestNotFoundError(PinterestError):
    """Resource not found."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


class PinterestAmbiguousWriteError(PinterestError):
    """A write was sent but we didn't receive a definitive response.

    The Pin MAY have been created. Do NOT retry blindly.
    Must be reconciled.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ProductionWriteDisabledError(PinterestError):
    """Raised when publishing or writing to Pinterest while publishing is disabled."""

    def __init__(
        self,
        message: str = (
            "Pinterest production write operations are disabled (PUBLISH_TO_PINTEREST=false)."
        ),
    ) -> None:
        super().__init__(message)
