"""Structured logging (structlog). JSON in CI/prod, readable console locally."""

from __future__ import annotations

import logging
import sys

import structlog

# Third-party loggers that print one INFO line per request or per import.
QUIET_LOGGERS = ("elastic_transport", "alembic.runtime.plugins")


def configure_logging(level: str = "INFO", json: bool = False, stderr: bool = False) -> None:
    """`stderr=True` keeps stdout free for a protocol (the MCP stdio transport)."""
    logging.basicConfig(format="%(message)s", level=level.upper(), stream=sys.stderr)
    for noisy in QUIET_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr if stderr else sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
