"""Drift detection: turn "which candidate fired" into a decision about the artifact.

`locator_usage` already says which strategy resolved each control, and `recoveries` says
which steps needed a recoverable outcome handled. Both are recorded for free on every replay.
This module is the part that acts on them: a non-primary candidate firing, or a step needing
a recovery it never needed before, is a signal that the surface has moved, and enough signal
demotes an approved capability back to `draft`.

Demotion is the whole point. A capability that passes while quietly resolving through its
third candidate is not healthy, it is one more UI change away from failing, and the moment to
notice is now rather than at 3am. Sending it back to `draft` costs a human review and stops
unattended replay - `Provenance.replayable_unattended` already refuses drafts, so nothing new
has to enforce it.

This deliberately consumes `StabilityReport` rather than recomputing per-control rates: the
measurement lives in one place and this is only the judgement on top of it.
"""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from cua.replay.stability import StabilityReport
from cua.schema.models import Capability

_log = logging.getLogger(__name__)

# A control resolving through a fallback in half its runs is not flaky, it is wrong: the
# recorded primary does not match this surface any more. Deliberately far above
# stability.NOTABLE_FALLBACK_RATE (0.2), which only decides whether to *mention* it - these
# two numbers answer different questions and should not be collapsed into one.
DEMOTION_RATE = 0.5

# One fallback is a flake, three runs of it is a pattern. Below this a verdict still lists
# every signal; it just will not act on them.
MIN_RUNS_TO_DEMOTE = 3


class DriftVerdict(BaseModel):
    """What the run(s) said about the artifact, and whether it may still run unattended."""

    capability_id: str
    capability_version: str
    tenant_id: str | None = None
    runs: int
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    signals: list[str] = Field(
        default_factory=list, description="Plain-language drift observations, worst first."
    )
    fallback_controls: dict[str, float] = Field(
        default_factory=dict,
        description="Controls whose non-primary rate crossed the demotion threshold.",
    )
    new_recoveries: dict[str, int] = Field(
        default_factory=dict,
        description="Steps that needed a recovery they have no baseline history of needing.",
    )
    demote: bool = Field(
        default=False, description="Whether these signals are enough to send it back to draft."
    )
    withheld: bool = Field(
        default=False,
        description=(
            "True when signals crossed the rate but there were too few runs to act on them."
        ),
    )
    reason: str | None = Field(default=None, description="Why it was demoted, for the human.")


def assess(capability: Capability, report: StabilityReport) -> DriftVerdict:
    """Judge a measured capability. Pure: it decides, it does not change anything."""
    # The baseline is what this capability is *known* to have needed. With no measurement on
    # record, every recovery is new by definition - which is the correct reading, not a gap.
    baseline_recoveries = set(capability.stability.recoveries) if capability.stability else set()

    signals: list[str] = []
    fallbacks: dict[str, float] = {}
    new_recoveries: dict[str, int] = {}

    for control in report.controls:
        if control.resolved_runs == 0:
            fallbacks[control.step_id] = 1.0
            signals.append(
                f"{control.step_id}: never resolved in any of {report.runs} run(s) - every "
                f"recorded candidate is dead against this surface."
            )
            continue
        rate = control.non_primary_rate
        if rate >= DEMOTION_RATE:
            fallbacks[control.step_id] = rate
            fired = ", ".join(f"{s} {n}x" for s, n in sorted(control.fired.items()))
            signals.append(
                f"{control.step_id}: resolved through a non-primary candidate in "
                f"{100 * rate:.0f}% of runs (primary={control.primary_strategy}; {fired})."
            )

    for step_id, count in sorted(report.recoveries.items()):
        if step_id in baseline_recoveries:
            continue
        rate = count / report.runs if report.runs else 0.0
        if rate >= DEMOTION_RATE:
            new_recoveries[step_id] = count
            signals.append(
                f"{step_id}: needed a recovery in {count} of {report.runs} run(s), and has no "
                f"baseline history of needing one."
            )

    crossed = bool(fallbacks or new_recoveries)
    enough_runs = report.runs >= MIN_RUNS_TO_DEMOTE
    demote = crossed and enough_runs and capability.provenance.state == "approved"

    reason = None
    if crossed and not enough_runs:
        signals.append(
            f"Signals crossed the threshold but only {report.runs} run(s) were measured; "
            f"{MIN_RUNS_TO_DEMOTE} are needed before demoting. Not acted on."
        )
    if demote:
        reason = "; ".join(signals)

    verdict = DriftVerdict(
        capability_id=capability.id,
        capability_version=capability.version,
        tenant_id=capability.app.tenant_id,
        runs=report.runs,
        signals=signals,
        fallback_controls=fallbacks,
        new_recoveries=new_recoveries,
        demote=demote,
        withheld=crossed and not enough_runs,
        reason=reason,
    )
    if signals:
        _log.info(
            "drift_detected",
            extra={
                "capability_id": capability.id,
                "tenant": capability.app.tenant_id,
                "runs": report.runs,
                "signal_count": len(signals),
                "demote": demote,
            },
        )
    return verdict


def demote(capability: Capability, verdict: DriftVerdict, report: StabilityReport) -> Capability:
    """Send a drifted capability back to draft, carrying the measurement that says why.

    Returns a new artifact rather than mutating: the caller decides whether this is written
    to disk, and a resolved tenant overlay is deliberately not written anywhere.
    """
    if not verdict.demote:
        return capability

    record = report.to_record()
    provenance = capability.provenance.model_copy(
        update={
            "state": "draft",
            # The invariant is that `approved_by` is null while draft. Keeping the old name
            # here would read as though that person signed off on the drifted version.
            "approved_by": None,
        }
    )
    return capability.model_copy(
        update={
            "provenance": provenance,
            "stability": record.model_copy(
                update={"drift_signals": [*record.drift_signals, *verdict.signals]}
            ),
        }
    )


def render(verdict: DriftVerdict) -> str:
    """A short console summary, for the CLI."""
    if not verdict.signals:
        return f"  no drift: {verdict.runs} run(s), every control resolved through its primary"

    lines = [f"  drift signals ({verdict.runs} run(s)):"]
    lines.extend(f"    - {signal}" for signal in verdict.signals)
    if verdict.demote:
        lines.append(f"  DEMOTED to draft: {verdict.capability_id} may no longer replay unattended")
    elif verdict.withheld:
        lines.append("  not demoted: too few runs to act on")
    else:
        lines.append("  not demoted: below the demotion threshold")
    return "\n".join(lines)
