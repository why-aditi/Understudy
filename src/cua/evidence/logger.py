"""Structured JSONL run logger with the redaction filter applied on write."""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel

from cua.policy.redaction import RedactionFilter

EVIDENCE_ROOT = Path("evidence")

LOGGER_NAME = "cua"

# Characters a Windows filename may not contain, plus control codes.
_UNSAFE_IN_FILENAME = re.compile("[" + re.escape('<>:"/\\|?*') + "]|[^ -~]")


# LogRecord's own attributes. Everything else on a record is a caller's structured field.

_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _jsonable(value: object) -> object:

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}

    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]

    if isinstance(value, str | int | float | bool | None):
        return value

    return str(value)


def _safe_filename(name: str) -> str:
    """Artifact names carry step ids, which are free text in the artifact.

    Windows rejects several characters outright, so a step called "<precondition>" would
    make the evidence write fail and lose the very failure it was recording.
    """
    cleaned = _UNSAFE_IN_FILENAME.sub("_", name).strip(". ")
    return cleaned or "artifact"


class JsonlHandler(logging.Handler):
    """Writes one JSON object per log record. The only sink in the system."""

    def __init__(self, path: Path) -> None:

        super().__init__()

        path.parent.mkdir(parents=True, exist_ok=True)

        self.path = path

        self._stream = path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:

        try:
            payload: dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
            }

            payload.update(
                {k: _jsonable(v) for k, v in record.__dict__.items() if k not in _STANDARD}
            )

            self._stream.write(json.dumps(payload) + "\n")

            self._stream.flush()

        except (TypeError, ValueError, OSError):
            self.handleError(record)

    def close(self) -> None:

        self._stream.close()

        super().close()


class RunLogger:
    """Owns one run's evidence directory and the JSONL sink every module logs into.



    Attaching to the `cua` logger means the policy engine and the surfaces write into the

    same file as the runner, already redacted, without knowing this class exists.

    """

    def __init__(self, run_id: str, root: Path = EVIDENCE_ROOT) -> None:

        self.run_id = run_id

        self.directory = root / run_id

        self.path = self.directory / "run.jsonl"

        self.redaction = RedactionFilter()

        self._handler: JsonlHandler | None = None

    def declare_sensitive(self, name: str, value: str) -> None:
        """Register a value that must never appear in the log."""

        self.redaction.declare(name, value)

    def save_artifact(self, name: str, data: bytes, subdir: str | None = None) -> Path:
        """Write a binary artifact beside the log and return its path.



        Screenshots live here and only here: they are never inlined into a record, so a

        log line can be read and shared without carrying a picture of a servicing screen.

        """

        directory = self.directory if subdir is None else self.directory / _safe_filename(subdir)

        directory.mkdir(parents=True, exist_ok=True)

        path = directory / _safe_filename(name)

        path.write_bytes(self._redacted(name, data))

        return path

    def _redacted(self, name: str, data: bytes) -> bytes:
        """Scrub a text artifact on its way to disk.

        The redaction filter sits on the log handler, so artifacts written as bytes used to
        escape it completely: a failure snapshot holds an `Observation.url`, and a url can
        carry a secret in its query string. Found when a capability with a sensitive
        parameter failed - the success path never writes either file.

        Screenshots are left alone. They are not text, and pixels cannot be scrubbed by
        string replacement, which is exactly why they are off by default.
        """
        if not name.endswith((".json", ".jsonl", ".txt")):
            return data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        return self.redaction.scrub_text(text).encode("utf-8")

    def event(self, name: str, **fields: object) -> None:
        """Write one structured record."""

        logging.getLogger(LOGGER_NAME).info(name, extra=fields)

    def __enter__(self) -> Self:

        handler = JsonlHandler(self.path)

        handler.addFilter(self.redaction)

        logger = logging.getLogger(LOGGER_NAME)

        logger.setLevel(logging.INFO)

        logger.addHandler(handler)

        self._handler = handler

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:

        if self._handler is not None:
            logging.getLogger(LOGGER_NAME).removeHandler(self._handler)

            self._handler.close()

            self._handler = None

        self.redaction.forget()
