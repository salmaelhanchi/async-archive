"""structlog to stderr, with the run id bound on every line."""

import logging
import secrets
import sys
from datetime import UTC, datetime

import structlog


class _CurrentStderr:
    """Writes to whatever ``sys.stderr`` is now. The live progress display swaps it for a
    proxy that prints above the bars; a logger holding the original would draw through them."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def new_run_id() -> str:
    """Sortable and unique: UTC start time plus random suffix."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def configure_logging(*, level: str, json_output: bool, run_id: str) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStderr()),  # type: ignore[arg-type]
        cache_logger_on_first_use=False,
    )
