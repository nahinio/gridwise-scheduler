"""Structured JSON logs on stdout with secret redaction.

Request bodies and note text are never logged (notes appear only as a short hash + length),
and anything that looks like a credential is masked before it is rendered.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any

import structlog

_SECRET_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|gsk_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_\-]{10,}|AQ\.[0-9A-Za-z_\-]{20,}|"
    r"Bearer\s+\S+)"
)
_MASK = "***"


def make_redactor(secret_values: Iterable[str]) -> structlog.typing.Processor:
    secrets = [value for value in secret_values if value]

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, _MASK)
            return _SECRET_PATTERNS.sub(_MASK, value)
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    def redact(
        _logger: Any, _method: str, event: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for key in list(event):
            event[key] = scrub(event[key])
        return event

    return redact


def configure_logging(level: str, secret_values: Iterable[str] = ()) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=numeric_level, format="%(message)s", force=True)
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            make_redactor(secret_values),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
