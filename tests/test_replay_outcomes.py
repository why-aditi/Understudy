"""Outcome evaluation inside the replay engine: what each of the three kinds actually does.

The taxonomy only earns its keep if the engine treats the three kinds differently, so these
tests are about the differences rather than about detection.
"""

import json
from pathlib import Path

from cua.evidence.logger import RunLogger
from cua.policy.engine import PolicyEngine
from cua.policy.rules import load_policy
from cua.replay.engine import ReplayEngine
from cua.schema.models import Condition, Outcome, Recovery, Step
from cua.surfaces.base import AXNode, Observation
from test_replay_engine import (
    REPO,
    URL,
    FakeSurface,
    anchor,
    build,
    capability,
)


def detector(text: str) -> Condition:
    return Condition(kind="text_present", params={"text": text})


def hard(name: str = "boom") -> list[Outcome]:
    return [
        Outcome(
            name=name,
            kind="hard_failure",
            detect=detector("Member 12345"),
            message_template="the session expired",
        )
    ]


# ---- hard_failure captures evidence ------------------------------------------------------


def test_a_hard_failure_captures_an_ax_snapshot(tmp_path: Path) -> None:
    """The snapshot is the observation the run was deciding from, and it carries no pixels."""
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=hard()), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None
    assert len(result.failure.evidence_paths) == 1

    snapshot = Path(result.failure.evidence_paths[0])
    assert snapshot.name.endswith(".ax.json")
    captured = json.loads(snapshot.read_text(encoding="utf-8"))
    assert captured["url"] == URL
    assert captured["tree"]["role"] == "RootWebArea"


def test_no_screenshot_unless_it_is_allowed(tmp_path: Path) -> None:
    """A failure screenshot of a servicing screen is unredacted PII, so it is opt-in."""
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=hard()), {"member_id": "12345"})

    assert result.failure is not None
    assert not any(p.endswith(".png") for p in result.failure.evidence_paths)
    assert list(logger.directory.glob("*.png")) == []


def test_a_screenshot_is_captured_when_allowed(tmp_path: Path) -> None:
    surface = FakeSurface()
    logger = RunLogger("replay-test", root=tmp_path)
    replay = ReplayEngine(
        surface=surface,
        policy=PolicyEngine(load_policy(REPO / "policy.yaml")),
        logger=logger,
        allow_screenshots=True,
        sleep=lambda _: None,
    )

    with logger:
        result = replay.run(capability(outcomes=hard()), {"member_id": "12345"})

    assert result.failure is not None
    assert any(p.endswith(".png") for p in result.failure.evidence_paths)


def test_every_failure_carries_the_candidates_it_tried(tmp_path: Path) -> None:
    """Not only an exhausted chain: which candidate fired is the first question about a bad run."""
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=hard()), {"member_id": "12345"})

    assert result.failure is not None
    assert result.failure.candidates_tried == ["anchor_relative"]
    assert result.failure.evidence_paths


def test_a_precondition_failure_captures_nothing(tmp_path: Path) -> None:
    """Nothing has been touched yet, so there is no screen worth freezing."""
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(approved=False), {"member_id": "12345"})

    assert result.failure is not None
    assert result.failure.evidence_paths == []


# ---- checkpoints and precedence ------------------------------------------------------------


def failing_checkpoint_step() -> list[Step]:
    return [
        Step(
            id="read-balance",
            intent="read the balance",
            action="extract",
            target=anchor("Savings Balance", "cell"),
            checkpoint=detector("nothing that is on this screen"),
        )
    ]


def test_a_checkpoint_failure_with_no_detector_is_a_hard_failure(tmp_path: Path) -> None:
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(steps=failing_checkpoint_step()), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None
    assert "no declared outcome explains it" in result.failure.observed
    assert result.failure.evidence_paths, "an unknown page state is exactly when evidence matters"


def test_a_declared_outcome_wins_over_a_failing_checkpoint(tmp_path: Path) -> None:
    """The capability classified this state; guessing from the checkpoint would discard that."""
    outcomes = [
        Outcome(
            name="member_not_found",
            kind="business",
            detect=detector("Member 12345"),
            message_template="No member matches that id.",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(
            capability(steps=failing_checkpoint_step(), outcomes=outcomes),
            {"member_id": "12345"},
        )

    assert result.status == "business_outcome"
    assert result.failure is None


# ---- recovery is bounded per outcome ---------------------------------------------------------


def test_recovery_budgets_are_per_outcome(tmp_path: Path) -> None:
    """Two recoverable conditions each get their declared budget; neither spends the other's."""
    outcomes = [
        Outcome(
            name="first",
            kind="recoverable",
            detect=detector("Member 12345"),
            recovery=Recovery(action="retry_step", max_attempts=1),
            message_template="first",
        ),
        Outcome(
            name="second",
            kind="recoverable",
            detect=detector("SAV-88120"),
            recovery=Recovery(action="retry_step", max_attempts=3),
            message_template="second",
        ),
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    # `first` is declared first so it always wins detection, and its own budget is 1.
    assert result.status == "failure"
    assert result.failure is not None
    assert "still firing after 1 of 1" in result.failure.observed


def test_each_recovery_attempt_is_recorded_as_drift(tmp_path: Path) -> None:
    outcomes = [
        Outcome(
            name="interstitial",
            kind="recoverable",
            detect=detector("Member 12345"),
            recovery=Recovery(action="wait_and_retry", max_attempts=2),
            message_template="a notice appeared",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    assert result.drift_signals == [
        "read-balance: recovered from 'interstitial' (attempt 1 of 2)",
        "read-balance: recovered from 'interstitial' (attempt 2 of 2)",
    ]


# ---- business outcomes carry only what they promised ------------------------------------------


def test_a_business_outcome_returns_the_outputs_it_declared_survive(tmp_path: Path) -> None:
    outcomes = [
        Outcome(
            name="partial",
            kind="business",
            detect=detector("Member 12345"),
            message_template="Only some of it.",
            partial_outputs=["balance"],
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    assert result.status == "business_outcome"
    assert result.outputs == {"balance": "4,182.55"}
    assert result.failure is None


def test_a_business_outcome_declaring_no_partial_outputs_returns_none(tmp_path: Path) -> None:
    outcomes = [
        Outcome(
            name="nothing_survives",
            kind="business",
            detect=detector("Member 12345"),
            message_template="Nothing to report.",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    assert result.status == "business_outcome"
    assert result.outputs is None


def test_a_business_outcome_is_recorded_in_the_evidence(tmp_path: Path) -> None:
    outcomes = [
        Outcome(
            name="member_not_found",
            kind="business",
            detect=detector("Member 12345"),
            message_template="No member matches that id.",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    events = [
        json.loads(line)["event"] for line in logger.path.read_text(encoding="utf-8").splitlines()
    ]
    assert "replay_business_outcome" in events


# ---- the clock -----------------------------------------------------------------------------


def test_a_failure_reports_a_real_duration(tmp_path: Path) -> None:
    """Every failure path used to stamp the clock as it failed, so duration was always 0ms."""
    clock = iter([0.0, 0.5, 1.0, 2.5, 4.0, 7.5, 9.0, 12.0])
    surface = FakeSurface()
    logger = RunLogger("replay-test", root=tmp_path)
    replay = ReplayEngine(
        surface=surface,
        policy=PolicyEngine(load_policy(REPO / "policy.yaml")),
        logger=logger,
        monotonic=lambda: next(clock),
        sleep=lambda _: None,
    )

    with logger:
        result = replay.run(capability(outcomes=hard()), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.duration_ms > 0


# ---- the run is self-documenting -------------------------------------------------------


def test_every_replay_writes_its_result_beside_the_log(tmp_path: Path) -> None:
    """run.jsonl is how the replay went; result.json is what the caller was told.

    Reading one without the other is guesswork, so the engine writes both rather than
    leaving it to whoever happened to call it.
    """
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(), {"member_id": "12345"})

    written = json.loads((logger.directory / "result.json").read_text(encoding="utf-8"))
    assert written == result.model_dump(mode="json")
    assert written["status"] == "success"
    assert (logger.directory / "run.jsonl").exists()


def test_a_failed_replay_also_leaves_a_result(tmp_path: Path) -> None:
    """The runs worth reading afterwards are the ones that went wrong."""
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=hard()), {"member_id": "12345"})

    written = json.loads((logger.directory / "result.json").read_text(encoding="utf-8"))
    assert written["status"] == "failure"
    assert written["failure"]["evidence_paths"] == result.failure.evidence_paths  # type: ignore[union-attr]


# ---- a checkpoint is waited for, not sampled once --------------------------------------------


class SlowSurface(FakeSurface):
    """Answers with the old screen for a few observations, then the new one - or never."""

    def __init__(self, arrives_after: int | None) -> None:
        super().__init__()
        self.arrives_after = arrives_after
        self.observations = 0

    def observe(self, *, screenshot: bool = False) -> Observation:
        self.observations += 1
        if self.arrives_after is not None and self.observations > self.arrives_after:
            self.tree = AXNode(role="RootWebArea", name="Verification result")
        return super().observe(screenshot=screenshot)


def submitting(**kwargs: object) -> Step:
    return Step(
        id="submit",
        intent="submit the form",
        action="click",
        target=anchor("Savings Balance", "cell"),
        **kwargs,  # type: ignore[arg-type]
    )


REACHED = Condition(kind="text_present", params={"text": "Verification result"})


def test_a_checkpoint_that_becomes_true_a_moment_later_still_passes(tmp_path: Path) -> None:
    """A submit that navigates leaves the old document in place for a few milliseconds.

    Sampling the checkpoint once immediately after the click reads it against the screen the
    step was trying to leave. Measured at 5 passes in 12 on a capability whose submit
    navigates; the same flow with no checkpoint on the acting step never showed it, because
    the assertion happened to land a step later.
    """
    surface = SlowSurface(arrives_after=2)
    engine, logger = build(surface, tmp_path)

    with logger:
        result = engine.run(
            capability(steps=[submitting(checkpoint=REACHED)], outputs=[], parameters=[]), {}
        )

    assert result.status == "success"
    assert surface.observations > 1, "it kept looking until the screen caught up"


def test_a_checkpoint_that_never_holds_still_fails_after_its_timeout(tmp_path: Path) -> None:
    """Waiting must not turn a real failure into a hang or a pass."""
    surface = SlowSurface(arrives_after=None)
    engine, logger = build(surface, tmp_path)

    with logger:
        result = engine.run(
            capability(
                steps=[submitting(checkpoint=REACHED, timeout_ms=200)], outputs=[], parameters=[]
            ),
            {},
        )

    assert result.status == "failure"
    assert result.failure is not None
    assert "checkpoint not met" in result.failure.observed


def test_a_step_with_no_checkpoint_observes_once(tmp_path: Path) -> None:
    """Nothing to wait for, and waiting on nothing would slow every extract down."""
    surface = SlowSurface(arrives_after=None)
    engine, logger = build(surface, tmp_path)

    with logger:
        engine.run(capability(steps=[submitting()], outputs=[], parameters=[]), {})

    # One before acting and one after: `_settle` looks once and returns, because there is
    # no condition to wait on.
    assert surface.observations == 2
