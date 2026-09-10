"""Drift detection: when measured signals are enough to stop trusting an artifact.

The case worth getting right is the quiet one - ten passing runs that all resolved through a
fallback. Nothing fails, nothing alerts, and the capability is one UI change from breaking.
These tests are mostly about that, and about not over-reacting to a single flake.
"""

from datetime import UTC, datetime

import pytest

from cua.replay.drift import (
    DEMOTION_RATE,
    MIN_RUNS_TO_DEMOTE,
    assess,
    demote,
    render,
)
from cua.replay.stability import StabilityReport, measure
from cua.schema.models import (
    Capability,
    OutputSpec,
    ReplayResult,
    StabilityRecord,
    Step,
)
from test_replay_engine import anchor, capability


def one_step(*, approved: bool = True, stability: StabilityRecord | None = None) -> Capability:
    """A capability whose single control's recorded primary is anchor_relative."""
    built = capability(
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
        approved=approved,
    )
    return built.model_copy(update={"stability": stability}) if stability else built


def result(
    *,
    usage: dict[str, str] | None = None,
    recoveries: dict[str, list[str]] | None = None,
) -> ReplayResult:
    return ReplayResult(
        status="success",
        capability_id="member.savings_balance.lookup",
        capability_version="1.0.0",
        run_id="r",
        outputs={"balance": "4,182.55"},
        steps_executed=1,
        duration_ms=100,
        locator_usage=usage if usage is not None else {"read-balance": "anchor_relative"},
        recoveries=recoveries or {},
    )


def report_of(*results: ReplayResult) -> StabilityReport:
    """Measure a scripted set of runs, exactly as the stability command would."""
    return measure(one_step(), lambda attempt: results[attempt], runs=len(results))


# ---- no drift ------------------------------------------------------------------------------


def test_every_control_on_its_primary_produces_no_signals() -> None:
    verdict = assess(one_step(), report_of(*[result() for _ in range(10)]))

    assert verdict.signals == []
    assert verdict.demote is False
    assert "no drift" in render(verdict)


def test_an_occasional_fallback_is_below_the_bar() -> None:
    """One flake in ten is not a dead primary, and demoting on it would cry wolf."""
    runs = [result() for _ in range(9)] + [result(usage={"read-balance": "text_content"})]

    verdict = assess(one_step(), report_of(*runs))

    assert verdict.signals == []
    assert verdict.demote is False


# ---- the quiet failure this exists to catch --------------------------------------------------


def test_ten_passing_runs_on_a_fallback_still_demote() -> None:
    """Nothing failed. The recorded primary is dead anyway, and that is the finding."""
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]
    report = report_of(*runs)
    assert report.passes == 10, "the premise: this is a fully passing capability"

    verdict = assess(one_step(), report)

    assert verdict.demote is True
    assert verdict.fallback_controls == {"read-balance": pytest.approx(1.0)}
    assert "non-primary" in verdict.signals[0]
    assert "DEMOTED" in render(verdict)


def test_exactly_at_the_rate_counts_as_crossing_it() -> None:
    runs = [
        result(usage={"read-balance": "anchor_relative" if i % 2 else "text_content"})
        for i in range(10)
    ]

    verdict = assess(one_step(), report_of(*runs))

    assert verdict.fallback_controls["read-balance"] == pytest.approx(DEMOTION_RATE)
    assert verdict.demote is True


def test_a_control_that_never_resolves_demotes() -> None:
    runs = [result(usage={}) for _ in range(5)]

    verdict = assess(one_step(), report_of(*runs))

    assert verdict.demote is True
    assert "never resolved" in verdict.signals[0]


# ---- newly-triggering recoverable outcomes ----------------------------------------------------


def test_a_recovery_with_no_baseline_history_is_drift() -> None:
    runs = [result(recoveries={"read-balance": ["maintenance_notice"]}) for _ in range(4)]

    verdict = assess(one_step(), report_of(*runs))

    assert verdict.new_recoveries == {"read-balance": 4}
    assert verdict.demote is True
    assert "no baseline history" in verdict.signals[0]


def test_a_recovery_the_capability_already_needed_is_not_new() -> None:
    """The word in the rule is *newly*. A known interstitial is not evidence of drift."""
    known = StabilityRecord(
        runs=10,
        passes=10,
        measured_at=datetime(2026, 9, 1, tzinfo=UTC),
        recoveries={"read-balance": 10},
    )
    runs = [result(recoveries={"read-balance": ["maintenance_notice"]}) for _ in range(4)]

    verdict = assess(one_step(stability=known), report_of(*runs))

    assert verdict.new_recoveries == {}
    assert verdict.demote is False


def test_a_rare_new_recovery_is_below_the_bar() -> None:
    runs = [result() for _ in range(9)] + [result(recoveries={"read-balance": ["x"]})]

    verdict = assess(one_step(), report_of(*runs))

    assert verdict.new_recoveries == {}
    assert verdict.demote is False


# ---- not over-reacting ------------------------------------------------------------------------


def test_too_few_runs_records_the_signal_but_withholds_the_demotion() -> None:
    """A single replay through a fallback is an observation, not a verdict."""
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(MIN_RUNS_TO_DEMOTE - 1)]

    verdict = assess(one_step(), report_of(*runs))

    assert verdict.signals, "the signal is still recorded"
    assert verdict.withheld is True
    assert verdict.demote is False
    assert "too few runs" in render(verdict)


def test_a_capability_already_in_draft_is_not_demoted_again() -> None:
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]

    verdict = assess(one_step(approved=False), report_of(*runs))

    assert verdict.signals, "still worth reporting"
    assert verdict.demote is False


# ---- what demotion does -----------------------------------------------------------------------


def test_demotion_sends_it_back_to_draft_and_blocks_unattended_replay() -> None:
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]
    report = report_of(*runs)
    original = one_step()
    assert original.provenance.replayable_unattended is True

    demoted = demote(original, assess(original, report), report)

    assert demoted.provenance.state == "draft"
    assert demoted.provenance.replayable_unattended is False


def test_demotion_clears_the_approver_rather_than_implying_they_signed_this_off() -> None:
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]
    report = report_of(*runs)
    original = one_step()

    demoted = demote(original, assess(original, report), report)

    assert demoted.provenance.approved_by is None


def test_the_demoted_artifact_carries_the_measurement_that_condemned_it() -> None:
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]
    report = report_of(*runs)
    original = one_step()

    demoted = demote(original, assess(original, report), report)

    assert demoted.stability is not None
    assert demoted.stability.runs == 10
    assert demoted.stability.locator_usage == {"read-balance": {"text_content": 10}}
    assert any("non-primary" in signal for signal in demoted.stability.drift_signals)


def test_demotion_does_not_mutate_the_capability_it_was_given() -> None:
    runs = [result(usage={"read-balance": "text_content"}) for _ in range(10)]
    report = report_of(*runs)
    original = one_step()

    demote(original, assess(original, report), report)

    assert original.provenance.state == "approved"


def test_a_clean_verdict_leaves_the_capability_alone() -> None:
    report = report_of(*[result() for _ in range(10)])
    original = one_step()

    assert demote(original, assess(original, report), report) is original


# ---- the recoveries the engine actually records -----------------------------------------------


def test_the_stability_report_counts_runs_that_needed_a_recovery_not_recoveries() -> None:
    """Two retries inside one run is still one misbehaving run."""
    runs = [result(recoveries={"read-balance": ["a", "b"]}), result()]

    assert report_of(*runs).recoveries == {"read-balance": 1}
