"""Redaction filter that strips sensitive values from anything written to disk."""

import logging
import re
from collections.abc import Mapping

from pydantic import BaseModel

# Declared values shorter than this are not redacted: blanking "1" would shred every record.
MIN_SENSITIVE_LENGTH = 4

# Shape-based backstop for values nobody declared. Order matters - the most specific
# shape must match first, or a card number gets labelled as an account.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("account", re.compile(r"\b\d{8,12}\b")),
)


def redact(text: str, sensitive: Mapping[str, str] | None = None) -> str:
    """Replace declared values and account-, SSN- and card-shaped strings with [REDACTED:name].

    Declared values go first so they are labelled with the parameter name the caller
    knows, rather than with whatever shape they happen to match.
    """
    for name, value in (sensitive or {}).items():
        if value and len(value) >= MIN_SENSITIVE_LENGTH:
            text = text.replace(value, f"[REDACTED:{name}]")

    for name, pattern in PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


class RedactionFilter(logging.Filter):
    """Sits on the log writer so no record can reach disk carrying a sensitive value.

    Sensitive parameter values are registered per invocation and never persisted, so this
    filter is the backstop for values that leak into a message, not the primary control.
    """

    def __init__(self, sensitive: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._sensitive: dict[str, str] = dict(sensitive or {})

    def declare(self, name: str, value: str) -> None:
        """Register one sensitive parameter value for the life of this run."""
        self._sensitive[name] = value

    def forget(self) -> None:
        """Drop every declared value. Called when a run ends."""
        self._sensitive.clear()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        if record.args is not None:
            record.args = self._scrub_args(record.args)
        for key, value in vars(record).items():
            if key not in _RESERVED:
                setattr(record, key, self._scrub(value))
        return True

    def scrub_text(self, text: str) -> str:
        """Apply this run's declared values to a blob of text.

        Evidence artifacts are written as bytes and never pass through a log record, so the
        filter has to be reachable directly or they escape it entirely.
        """
        return redact(text, self._sensitive)

    def _scrub(self, value: object) -> object:
        if isinstance(value, str):
            return redact(value, self._sensitive)
        if isinstance(value, BaseModel):
            # A model reaches a record whole - `logger.event("replay_end", result=result)` -
            # and walking only dicts and lists let every string inside one straight through.
            # `FailureDetail.observed` is where a url carrying a query-string secret lands.
            return value.model_validate(self._scrub(value.model_dump(mode="json")))
        if isinstance(value, Mapping):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._scrub(v) for v in value)
        return value

    def _scrub_args(
        self, args: tuple[object, ...] | Mapping[str, object]
    ) -> tuple[object, ...] | Mapping[str, object]:
        """logging requires args to stay a tuple or a mapping, so scrub in place of shape."""
        if isinstance(args, tuple):
            return tuple(self._scrub(value) for value in args)
        return {key: self._scrub(value) for key, value in args.items()}


# LogRecord's own attributes: scrubbing these would corrupt the record itself.
_RESERVED = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)
