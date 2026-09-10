"""Measuring determinism: what the numbers are, and whether they are reported honestly.

The aggregation takes a `replay_once` callable, so these run with no browser at all - which
is what makes it practical to test the awkward cases, like a fallback firing on some runs
and not others.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from cua.replay.stability import (
    NOTABLE_FALLBACK_RATE,
    ControlStability,
    StabilityReport,
    measure,
    render,
    write_report,
)
from cua.schema.models import (
    Capability,
    FailureDetail,
    OutcomeResult,
    OutputSpec,
    ReplayResult,
    Step,
)
from test_replay_engine import anchor, capability


def result(
    status: str = "success",
    *,
    usage: dict[str, str] | None = None,
    drift: list[str] | None = None,
    failure: FailureDetail | None = None,
    duration: int = 100,
) -> ReplayResult:
    return ReplayResult(
        status=status,  # type: ignore[arg-type]
        capability_id="member.savings_balance.lookup",
        capability_version="1.0.0",
        run_id="r",
        outputs={"balance": "4,182.55"} if status == "success" else None,
        failure=failure,
        outcome=None,
        steps_executed=1,
        duration_ms=duration,
        locator_usage=usage if usage is not None else {"read-balance": "anchor_relative"},
        drift_signals=drift or [],
    )


def one_step() -> Capability:
    """A capability whose single control's primary is anchor_relative."""
    return capability(
        steps=[
            Step(
                id="read-balance",
                intent="read the balance",
                action="extract",
                target=anchor("Savings Balance", "cell"),
            )
        ],
        outputs=[
            OutputSpec(
                name="balance", type="string", source_step_id="read-balance", description="d"
            )
        ],
    )


def replaying(*results: ReplayResult) -> Callable[[int], ReplayResult]:
    """A replay_once that serves scripted results in order."""

    def replay_once(attempt: int) -> ReplayResult:
        return results[attempt]

    return replay_once


# ---- the happy measurement -------------------------------------------------------------


def test_ten_identical_runs_are_reported_as_deterministic() -> None:
    report = measure(one_step(), replaying(*[result() for _ in range(10)]), runs=10)

    assert report.runs == 10
    assert report.passes == 10
    assert report.pass_rate == 1.0
    assert report.statuses == {"success": 10}
    assert report.deterministic is True
    assert any("same status" in finding for finding in report.findings)


def test_the_primary_firing_every_time_raises_no_finding_about_ranking() -> None:
    report = measure(one_step(), replaying(*[result() for _ in range(5)]), runs=5)

    control = report.controls[0]
    assert control.primary_strategy == "anchor_relative"
    assert control.fired == {"anchor_relative": 5}
    assert control.non_primary_rate == 0.0


# ---- the finding the command exists to surface -------------------------------------------


def test_a_fallback_firing_regularly_is_reported_not_hidden() -> None:
    """A capability can pass ten out of ten while its recorded primary is already dead."""
    runs = [
        result(usage={"read-balance": "anchor_relative" if i < 3 else "text_content"})
        for i in range(10)
    ]

    report = measure(one_step(), replaying(*runs), runs=10)

    assert report.passes == 10, "every run succeeded, which is exactly why this is easy to miss"
    control = report.controls[0]
    assert control.fired == {"anchor_relative": 3, "text_content": 7}
    assert control.non_primary_rate == pytest.approx(0.7)

    finding = next(f for f in report.findings if "read-balance" in f)
    assert "recorded primary" in finding
    assert "text_content 7x" in finding
    assert "does not actually favour" in finding


def test_a_passing_run_with_a_fallback_is_not_called_deterministic() -> None:
    """Ten passes that resolve through different candidates are ten passes and no determinism."""
    runs = [
        result(usage={"read-balance": "anchor_relative" if i % 2 else "text_content"})
        for i in range(10)
    ]

    report = measure(one_step(), replaying(*runs), runs=10)

    assert report.passes == 10
    assert report.deterministic is False


def test_an_occasional_fallback_is_still_mentioned() -> None:
    """Below the notable threshold it is not a ranking complaint, but it is still not silence."""
    runs = [result() for _ in range(9)] + [result(usage={"read-balance": "role_name"})]

    report = measure(one_step(), replaying(*runs), runs=10)

    assert report.controls[0].non_primary_rate == pytest.approx(0.1)
    assert report.controls[0].non_primary_rate < NOTABLE_FALLBACK_RATE
    assert any("more than one strategy" in f for f in report.findings)


def test_a_control_that_never_resolves_says_so_plainly() -> None:
    """The real case found by running this against a member the capability was not recorded on."""
    detail = FailureDetail(
        step_id="read-balance",
        expected="a control matching cell",
        observed="role_name matched 0; anchor_relative matched 0",
        candidates_tried=["role_name", "anchor_relative"],
    )
    runs = [result("failure", usage={}, failure=detail) for _ in range(10)]

    report = measure(one_step(), replaying(*runs), runs=10)

    assert report.passes == 0
    control = report.controls[0]
    assert control.resolved_runs == 0
    assert control.non_primary_rate == 0.0, "never resolving is not a ranking complaint"
    assert any("never resolved in any run" in f for f in report.findings)


# ---- failures and drift -----------------------------------------------------------------


def test_failures_are_collected_with_their_run_number() -> None:
    detail = FailureDetail(step_id="read-balance", expected="x", observed="chain exhausted")
    runs = [result(), result("failure", failure=detail), result()]

    report = measure(one_step(), replaying(*runs), runs=3)

    assert report.passes == 2
    assert report.statuses == {"success": 2, "failure": 1}
    assert report.failures == ["run 2: read-balance: chain exhausted"]


def test_identical_failures_collapse_in_the_summary() -> None:
    """Ten identical failures are one finding, not ten lines of the same sentence."""
    detail = FailureDetail(step_id="read-balance", expected="x", observed="chain exhausted")
    runs = [result("failure", failure=detail) for _ in range(10)]

    rendered = render(measure(one_step(), replaying(*runs), runs=10))

    assert "10x read-balance: chain exhausted" in rendered
    assert rendered.count("chain exhausted") == 1


def test_drift_signals_are_gathered_across_runs() -> None:
    runs = [result(drift=["read-balance: fell back to text_content at rank 1"]) for _ in range(3)]

    report = measure(one_step(), replaying(*runs), runs=3)

    assert len(report.drift_signals) == 3


def test_business_outcomes_are_not_counted_as_passes() -> None:
    """A capability that always returns "no such member" is stable, but it is not passing."""
    runs = [
        ReplayResult(
            status="business_outcome",
            capability_id="c",
            capability_version="1.0.0",
            run_id="r",
            outcome=OutcomeResult(name="member_not_found", kind="business", message="none"),
            steps_executed=1,
            duration_ms=10,
        )
        for _ in range(4)
    ]

    report = measure(one_step(), replaying(*runs), runs=4)

    assert report.passes == 0
    assert report.statuses == {"business_outcome": 4}
    assert report.deterministic is True, "consistent behaviour, just not success"


# ---- the artifact it writes ----------------------------------------------------------------


def test_the_report_is_written_where_the_runs_can_be_found(tmp_path: Path) -> None:
    report = measure(one_step(), replaying(*[result() for _ in range(10)]), runs=10)

    path = write_report(report, tmp_path)

    assert path.name == "replay-x10.json"
    reloaded = StabilityReport.model_validate_json(path.read_text(encoding="utf-8"))
    assert reloaded.runs == 10
    assert reloaded.controls == report.controls


def test_the_report_converts_to_the_schema_s_stability_record() -> None:
    """So a measured capability can carry its own evidence."""
    report = measure(one_step(), replaying(*[result() for _ in range(10)]), runs=10)

    record = report.to_record()

    assert record.runs == 10
    assert record.passes == 10
    assert record.pass_rate == 1.0
    assert record.locator_usage == {"read-balance": {"anchor_relative": 10}}


def test_the_console_summary_leads_with_what_fired(tmp_path: Path) -> None:
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]

    rendered = render(measure(one_step(), replaying(*runs), runs=10))

    assert "pass rate    10/10 (100%)" in rendered
    assert "primary=anchor_relative" in rendered
    assert "text_content 10x" in rendered
    assert "<-- fallback" in rendered, "a fallback must be visible at a glance"


def test_zero_runs_does_not_divide_by_zero() -> None:
    report = measure(one_step(), replaying(), runs=0)
    assert report.pass_rate == 0.0
    assert render(report)


def test_control_stability_rate_is_zero_when_nothing_resolved() -> None:
    control = ControlStability(step_id="s", primary_strategy="role_name")
    assert control.non_primary_rate == 0.0
