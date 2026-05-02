"""
src/utils/logger.py
====================
Structured JSON logging subsystem.

Design decisions:
  - Every log line is a JSON object — ingestible by Splunk/Datadog/CloudWatch
    without a parsing rule.
  - Sensitive headers (Authorization, Cookie, X-API-Key, etc.) are masked
    BEFORE they ever reach the log sink. Credentials cannot leak through logs.
  - Each test run gets a unique `run_id` bound to every log line for
    cross-filtering in log aggregation systems.
  - Correlation IDs are propagated from the HTTP client through to the log,
    allowing a full request trace to be reconstructed from logs alone.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SENSITIVE_HEADER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"^authorization$",
        r"^x-api-key$",
        r"^cookie$",
        r"^set-cookie$",
        r"^x-auth-token$",
        r"^proxy-authorization$",
    ]
]

_MASK = "***REDACTED***"

# One unique ID per test-runner process invocation.
RUN_ID: str = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

def _mask_sensitive_headers(
    logger: WrappedLogger, method: str, event_dict: EventDict
) -> EventDict:
    """
    Structlog processor: redacts sensitive HTTP headers in-place.
    Applied before any sink — headers are NEVER written unmasked.
    """
    for key in ("request_headers", "response_headers"):
        headers: dict[str, str] | None = event_dict.get(key)
        if not headers:
            continue
        masked: dict[str, str] = {}
        for header_name, header_value in headers.items():
            if any(p.match(header_name) for p in _SENSITIVE_HEADER_PATTERNS):
                masked[header_name] = _MASK
            else:
                masked[header_name] = header_value
        event_dict[key] = masked
    return event_dict


def _inject_run_id(
    logger: WrappedLogger, method: str, event_dict: EventDict
) -> EventDict:
    """Bind the process-level run_id to every log record."""
    event_dict.setdefault("run_id", RUN_ID)
    return event_dict


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def configure_logging(log_file: str, log_level: str = "INFO") -> None:
    """
    Call once at session startup (conftest.py).
    Configures structlog to emit JSON to both stderr and a JSONL file.
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # ---- stdlib logging → structlog bridge ----
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )

    # File handler: append JSONL for the entire session
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(level)
    logging.getLogger().addHandler(file_handler)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_run_id,
        _mask_sensitive_headers,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Factory — get a named, pre-bound logger instance."""
    return structlog.get_logger(name)
