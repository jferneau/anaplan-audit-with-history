"""Structured logging configuration using structlog.

Generates a per-run correlation ID and binds it (along with the tenant name)
to every log event.  JSON output to stderr by default; ``--verbose`` switches
to rich-rendered console output.
"""

from __future__ import annotations

import logging
import uuid

import structlog


def configure_logging(*, verbose: bool, tenant_name: str) -> structlog.stdlib.BoundLogger:
    """Initialise structlog and return a bound logger for the current run.

    Args:
        verbose: When *True*, use coloured console renderer instead of JSON.
        tenant_name: Anaplan tenant name bound to every log event.

    Returns:
        A :class:`structlog.stdlib.BoundLogger` pre-bound with ``run_id``
        and ``tenant_name``.
    """
    run_id = str(uuid.uuid4())

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if verbose:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging so httpx etc. flow through structlog.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    logger: structlog.stdlib.BoundLogger = structlog.get_logger()
    logger = logger.bind(run_id=run_id, tenant_name=tenant_name)
    return logger
