"""InterventionRequest: the record written when a run cannot proceed without a human.

The request is the handover note. An operator arrives with no context at all - they did not
watch the run, they may not know the application - so it has to say what was being attempted,
where it stopped, why, and what the screen looked like at that moment.

It is written to disk *before* the lock is released. If the process died between those two
steps, the alternative order would leave a session nobody owns and no record of why.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from cua.session.lock import LockState

_log = logging.getLogger(__name__)

INTERVENTION_FILE = "intervention.json"
RESUME_FILE = "resume.json"

Reason = Literal["stuck", "risky_action_blocked", "unrecoverable", "policy_block"]


class HumanAction(BaseModel):
    """One thing a human did while holding the lock.

    Input values are never carried here. The page-side listener sends the length and nothing
    else, so a password typed during a handoff cannot reach the log even by accident - there
    is no redaction step to forget, because the value never leaves the browser.
    """

    kind: Literal["click", "input", "navigate", "submit"]
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    frame: str = Field(default="", description="Frame url or name the action happened in.")
    role: str | None = None
    name: str | None = None
    url: str | None = None
    value_length: int | None = Field(
        default=None, description="Characters typed. The value itself is never captured."
    )

    def describe(self) -> str:
        where = f" in frame {self.frame}" if self.frame else ""
        if self.kind == "navigate":
            return f"navigated to {self.url}{where}"
        if self.kind == "input":
            return f"typed {self.value_length} character(s) into {self.role} {self.name!r}{where}"
        return f"{self.kind} {self.role} {self.name!r}{where}"


class InterventionRequest(BaseModel):
    """Everything an operator needs to pick up a stalled run."""

    run_id: str
    reason: Reason = Field(
        description=(
            "stuck: no progress. risky_action_blocked: the policy refused an irreversible "
            "action. unrecoverable: an outcome the capability cannot handle. policy_block: "
            "refused for another policy reason."
        )
    )
    step_id: str
    message: str = Field(description="What the run was trying to do, in an operator's terms.")
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    capability_id: str | None = None
    capability_version: str | None = None
    goal: str | None = None

    url: str = Field(default="", description="Where the session was when it stopped.")
    ax_snapshot_path: str | None = Field(
        default=None, description="Accessibility snapshot of the screen at the moment it stopped."
    )
    screenshot_ref: str | None = Field(
        default=None, description="Screenshot path, present only when screenshots are permitted."
    )
    lock: LockState | None = Field(
        default=None, description="State of the session lock as the request was written."
    )

    resolved_at: datetime | None = None
    resumed_by: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.resolved_at is None


def write_request(request: InterventionRequest, directory: Path) -> Path:
    """Persist the request. Written before the lock is released, never after."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / INTERVENTION_FILE
    path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
    _log.info(
        "intervention_written",
        extra={"run_id": request.run_id, "reason": request.reason, "step": request.step_id},
    )
    return path


def read_request(directory: Path) -> InterventionRequest | None:
    """The pending request for a run, or None if there is not one."""
    path = directory / INTERVENTION_FILE
    if not path.exists():
        return None
    return InterventionRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))


class ResumeSignal(BaseModel):
    """An operator saying "I am done, take it back".

    A file rather than a call, because the console and the run are separate processes: the
    console holds no reference to the run, and the run may be waiting on a machine the
    console never talks to directly. A file both can see is the smallest thing that works.
    """

    run_id: str
    by: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def signal_resume(directory: Path, by: str) -> Path:
    """Record that a human has finished and control should return to automation."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RESUME_FILE
    signal = ResumeSignal(run_id=directory.name, by=by)
    path.write_text(signal.model_dump_json(indent=2), encoding="utf-8")
    _log.info("resume_signalled", extra={"run_id": signal.run_id, "by": by})
    return path


def resume_signal(directory: Path) -> ResumeSignal | None:
    """The pending resume signal for a run, if an operator has given one."""
    path = directory / RESUME_FILE
    if not path.exists():
        return None
    return ResumeSignal.model_validate(json.loads(path.read_text(encoding="utf-8")))


def clear_resume(directory: Path) -> None:
    """Consume the signal, so the next escalation waits for a fresh one."""
    (directory / RESUME_FILE).unlink(missing_ok=True)
