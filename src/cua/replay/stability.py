"""Replay a capability N times and measure whether it actually behaves the same way.

"Replay is deterministic" is a claim. This turns it into a number, and it is cheap to do
because replay costs no model calls - the whole economic argument for the artifact is that
running it forty times is free.

The measurement that matters most is not the pass rate. It is which locator candidate fired
each time. A capability can pass ten out of ten while quietly resolving through its third
candidate every single run, which means the recorded primary is already dead and the chain
is the only thing holding the capability up. That is a finding about our own ranking
heuristic, so it is reported at the top rather than buried: `non_primary_rate` per control,
and a `findings` list written in plain sentences.
"""

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from cua.schema.models import Capability, ReplayResult, StabilityRecord

_log = logging.getLogger(__name__)

STABILITY_DIR = Path("evidence/stability")

# A fallback firing this often is not noise. It means the recorded primary is gone and the
# capability is running on a candidate nobody ranked first.
NOTABLE_FALLBACK_RATE = 0.2


class ControlStability(BaseModel):
    """How one control resolved across the runs."""

    step_id: str
    primary_strategy: str = Field(description="What the artifact ranks first for this control.")
    fired: dict[str, int] = Field(
        default_factory=dict, description="How often each strategy actually resolved it."
    )
    resolved_runs: int = Field(default=0, description="Runs in which this control resolved at all.")

    @property
    def non_primary_rate(self) -> float:
        """Fraction of resolutions that fell through to a candidate below the primary."""
        if not self.resolved_runs:
            return 0.0
        return 1.0 - (self.fired.get(self.primary_strategy, 0) / self.resolved_runs)


class StabilityReport(BaseModel):
    """The measurement, written to evidence so the claim can be checked rather than believed."""

    capability_id: str
    capability_version: str
    runs: int
    passes: int
    measured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    statuses: dict[str, int] = Field(
        default_factory=dict, description="How many runs ended in each status."
    )
    controls: list[ControlStability] = Field(default_factory=list)
    recoveries: dict[str, int] = Field(
        default_factory=dict,
        description="Per step, how many runs needed a recovery to get past an outcome.",
    )
    drift_signals: list[str] = Field(default_factory=list)
    durations_ms: list[int] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    findings: list[str] = Field(
        default_factory=list,
        description="Plain-language observations, including ones unflattering to the ranking.",
    )

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    @property
    def deterministic(self) -> bool:
        """Whether every run did the same thing, not merely whether every run worked.

        Ten passes that each resolved through a different candidate are ten passes and no
        determinism at all.
        """
        return len(self.statuses) == 1 and all(len(c.fired) <= 1 for c in self.controls)

    def to_record(self) -> StabilityRecord:
        """The schema's own StabilityRecord, for embedding in a capability."""
        return StabilityRecord(
            runs=self.runs,
            passes=self.passes,
            measured_at=self.measured_at,
            locator_usage={c.step_id: dict(c.fired) for c in self.controls},
            recoveries=self.recoveries,
            drift_signals=self.drift_signals,
        )


def _findings(report: StabilityReport) -> list[str]:
    """Say what the numbers mean, including when they are unflattering."""
    notes: list[str] = []

    if report.runs and report.passes < report.runs:
        notes.append(
            f"{report.runs - report.passes} of {report.runs} runs did not succeed: "
            + ", ".join(f"{count}x {status}" for status, count in sorted(report.statuses.items()))
        )

    for control in report.controls:
        if control.resolved_runs == 0:
            notes.append(
                f"{control.step_id}: never resolved in any run - the whole candidate chain was "
                f"exhausted every time, so nothing recorded for this control works against the "
                f"screen as it is now."
            )
            continue

        rate = control.non_primary_rate
        if rate >= NOTABLE_FALLBACK_RATE:
            fired = ", ".join(f"{s} {n}x" for s, n in sorted(control.fired.items()))
            notes.append(
                f"{control.step_id}: the recorded primary ({control.primary_strategy}) resolved "
                f"only {100 * (1 - rate):.0f}% of the time - {fired}. The ranking put a "
                f"candidate first that this surface does not actually favour."
            )
        elif len(control.fired) > 1:
            notes.append(
                f"{control.step_id}: resolved through more than one strategy across runs "
                f"({', '.join(sorted(control.fired))}), so the surface is not answering "
                f"identically every time."
            )

    if report.deterministic and report.passes == report.runs:
        notes.append(
            f"All {report.runs} runs produced the same status and resolved every control "
            f"through the same candidate."
        )
    return notes


def measure(
    capability: Capability,
    replay_once: Callable[[int], ReplayResult],
    *,
    runs: int = 10,
) -> StabilityReport:
    """Run the capability `runs` times and aggregate what happened.

    `replay_once` performs one replay and returns its result. Passing it in keeps this
    function free of browsers, sessions and policy - and makes the aggregation testable
    without any of them.
    """
    primaries = {step.id: step.target.primary.strategy for step in capability.steps if step.target}
    fired: dict[str, Counter[str]] = defaultdict(Counter)
    statuses: Counter[str] = Counter()
    drift: list[str] = []
    recoveries: Counter[str] = Counter()
    durations: list[int] = []
    failures: list[str] = []

    for attempt in range(runs):
        result = replay_once(attempt)
        statuses[result.status] += 1
        durations.append(result.duration_ms)
        drift.extend(result.drift_signals)
        # Counting *runs that needed a recovery*, not individual recoveries: a step that
        # retried twice in one run is still one run in which the surface misbehaved.
        recoveries.update(result.recoveries.keys())
        if result.status == "failure" and result.failure is not None:
            failures.append(
                f"run {attempt + 1}: {result.failure.step_id}: {result.failure.observed}"
            )
        for step_id, strategy in result.locator_usage.items():
            fired[step_id][strategy] += 1
        _log.info(
            "stability_run",
            extra={
                "attempt": attempt + 1,
                "of": runs,
                "status": result.status,
                "duration_ms": result.duration_ms,
            },
        )

    controls = [
        ControlStability(
            step_id=step_id,
            primary_strategy=primaries.get(step_id, "unknown"),
            fired=dict(fired[step_id]),
            resolved_runs=sum(fired[step_id].values()),
        )
        for step_id in sorted(set(primaries) | set(fired))
    ]

    report = StabilityReport(
        capability_id=capability.id,
        capability_version=capability.version,
        runs=runs,
        passes=statuses.get("success", 0),
        statuses=dict(statuses),
        controls=controls,
        recoveries=dict(recoveries),
        drift_signals=drift,
        durations_ms=durations,
        failures=failures,
    )
    return report.model_copy(update={"findings": _findings(report)})


def write_report(report: StabilityReport, directory: Path = STABILITY_DIR) -> Path:
    """Persist the measurement next to the runs it summarises."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"replay-x{report.runs}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def render(report: StabilityReport) -> str:
    """A short console summary. The findings come first because they are the point."""
    lines = [
        f"{report.capability_id} v{report.capability_version}",
        f"  runs         {report.runs}",
        f"  pass rate    {report.passes}/{report.runs} ({100 * report.pass_rate:.0f}%)",
        "  statuses     " + ", ".join(f"{k} {v}" for k, v in sorted(report.statuses.items())),
        f"  deterministic {report.deterministic}",
    ]
    if report.durations_ms:
        median = sorted(report.durations_ms)[len(report.durations_ms) // 2]
        lines.append(f"  duration     median {median} ms")

    lines.append("")
    lines.append("  per control, which candidate actually fired:")
    for control in report.controls:
        fired = ", ".join(f"{s} {n}x" for s, n in sorted(control.fired.items())) or "never resolved"
        flag = "  <-- fallback" if control.non_primary_rate >= NOTABLE_FALLBACK_RATE else ""
        lines.append(
            f"    {control.step_id:<20} primary={control.primary_strategy:<16} {fired}{flag}"
        )

    if report.findings:
        lines.append("")
        lines.append("  findings:")
        lines.extend(f"    - {finding}" for finding in report.findings)
    if report.failures:
        lines.append("")
        lines.append("  failures:")
        # Ten identical failures are one finding, not ten. Collapsing them keeps the
        # interesting case - runs failing in *different* ways - visible.
        for detail, count in Counter(_shape(f) for f in report.failures).most_common(5):
            lines.append(f"    - {count}x {detail}")
    return "\n".join(lines)


def _shape(failure: str) -> str:
    """A failure with its run number stripped, so identical failures collapse together."""
    return failure.split(": ", 1)[1] if failure.startswith("run ") else failure
