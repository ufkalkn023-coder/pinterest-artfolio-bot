"""Structured logging configuration.

Uses structlog for structured logging with rich contextual fields.
Supports JSON (production) and human-readable (development) output formats.

Logs are written to stderr so that stdout stays reserved for command output
(including `--json` payloads, which must remain machine-parseable).

CRITICAL: Never log access tokens or secrets.
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
) -> None:
    """Configure structured logging for the application.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).
        fmt: Output format — "json" for machine-readable, "console" for dev.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if fmt == "console":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
