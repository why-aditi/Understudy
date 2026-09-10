"""Deterministic replay: same artifact, same screen, same actions, no model anywhere."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cua.evidence.logger import RunLogger
from cua.policy.engine import PolicyEngine
from cua.policy.rules import load_policy
from cua.replay.engine import (
    ParameterError,
    ReplayEngine,
    ReplayError,
    bind_descriptor,
    bind_locator,
    can_act_through,
    load_capability,
    to_action_target,
    validate_params,
)
from cua.schema.models import (
    AppRef,
    Capability,
    Condition,
    ControlDescriptor,
    EntryPoint,
    Locator,
    Outcome,
    OutputSpec,
    Parameter,
    ParamRef,
    Provenance,
    Recovery,
    Step,
)
from cua.surfaces.base import Action, ActionResult, AXNode, Observation, PruningStats

REPO = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8099/tenant-a/members/12345"


def node(role: str, name: str | None = None, *children: AXNode, value: str | None = None) -> AXNode:
    return AXNode(role=role, name=name, value=value, children=list(children))


def anchor(anchor_text: str, target_role: str = "link") -> ControlDescriptor:
    return ControlDescriptor(
        role=target_role,
        name="Open",
        candidates=[
            Locator(
                strategy="anchor_relative",
                params={
                    "anchor_text": anchor_text,
                    "relation": "same_row",
                    "target_role": target_role,
                    "index": 0,
                },
                stability_score=0.9,
                verified_unique_at_record=True,
            )
        ],
    )


SCREEN = node(
    "RootWebArea",
    "Member 12345",
    node(
        "table",
        None,
        node(
            "row",
            None,
            node("cell", "SAV-88120"),
            node("cell", "Savings Balance"),
            node("cell", "4,182.55"),
            node("cell", "Open", node("link", "Open")),
        ),
    ),
    node("textbox", "Member ID", value="12345"),
)


class FakeSurface:
    """Records what it was asked to do and answers with a scripted screen."""

    def __init__(self, tree: AXNode = SCREEN, *, extract: str = "4,182.55") -> None:
        self.tree = tree
        self.extract = extract
        self.actions: list[Action] = []
        self.fail_next: str | None = None

    def observe(self, *, screenshot: bool = False) -> Observation:
        return Observation(
            url=URL,
            title="Member",
            tree=self.tree,
            observation_hash="h",
            pruning=PruningStats(nodes_before=1, nodes_after=1),
            screenshot=b"fake-png-bytes" if screenshot else None,
        )

    def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        error, self.fail_next = self.fail_next, None
        return ActionResult(
            action=action,
            ok=error is None,
            url_after=URL,
            duration_ms=1,
            extracted=self.extract if action.kind == "extract" else None,
            error=error,
        )


def provenance(*, approved: bool = True) -> Provenance:
    return Provenance(
        discovered_at=datetime(2026, 9, 10, tzinfo=UTC),
        model="fake",
        discovery_run_id="r",
        state="approved" if approved else "draft",
        approved_by="aditi" if approved else None,
        outcomes_reviewed=approved,
    )


def capability(
    *,
    steps: list[Step] | None = None,
    outcomes: list[Outcome] | None = None,
    outputs: list[OutputSpec] | None = None,
    parameters: list[Parameter] | None = None,
    approved: bool = True,
) -> Capability:
    return Capability(
        id="member.savings_balance.lookup",
        name="Look up savings balance",
        description="Read a member's savings balance.",
        version="1.0.0",
        app=AppRef(vendor_product="meridian-core"),
        entry=EntryPoint(url=URL),
        parameters=parameters
        if parameters is not None
        else [
            Parameter(
                name="member_id",
                type="string",
                required=True,
                description="Member id.",
            )
        ],
        outputs=outputs
        if outputs is not None
        else [
            OutputSpec(
                name="balance",
                type="string",
                source_step_id="read-balance",
                description="The balance.",
            )
        ],
        steps=steps
        if steps is not None
        else [
            Step(
                id="read-balance",
                intent="read the balance",
                action="extract",
                target=anchor("Savings Balance", "cell"),
            )
        ],
        outcomes=outcomes or [],
        provenance=provenance(approved=approved),
    )


@pytest.fixture
def engine(tmp_path: Path) -> ReplayEngine:
    return ReplayEngine(
        surface=FakeSurface(),
        policy=PolicyEngine(load_policy(REPO / "policy.yaml")),
        logger=RunLogger("replay-test", root=tmp_path),
        sleep=lambda _: None,
    )


def build(surface: FakeSurface, tmp_path: Path) -> tuple[ReplayEngine, RunLogger]:
    logger = RunLogger("replay-test", root=tmp_path)
    return (
        ReplayEngine(
            surface=surface,
            policy=PolicyEngine(load_policy(REPO / "policy.yaml")),
            logger=logger,
            sleep=lambda _: None,
        ),
        logger,
    )


# ---- parameters are checked before anything is touched -------------------------------------------


def test_a_missing_required_parameter_is_refused() -> None:
    declared = [Parameter(name="member_id", type="string", required=True, description="id")]
    with pytest.raises(ParameterError, match="missing required parameter"):
        validate_params(declared, {})


def test_an_unknown_parameter_is_refused() -> None:
    """A misspelled name would otherwise run the capability with a silent default."""
    declared = [Parameter(name="member_id", type="string", required=True, description="id")]
    with pytest.raises(ParameterError, match="unknown parameter"):
        validate_params(declared, {"member_id": "1", "membr_id": "2"})


@pytest.mark.parametrize(
    ("declared", "given", "expected"),
    [
        ("string", 12345, "12345"),
        ("integer", "42", 42),
        ("number", "4.5", 4.5),
        ("boolean", "yes", True),
        ("boolean", "no", False),
        ("date", "2026-09-10", "2026-09-10"),
    ],
)
def test_values_are_coerced_to_their_declared_type(
    declared: str, given: object, expected: object
) -> None:
    parameter = Parameter(
        name="p",
        type=declared,  # type: ignore[arg-type]
        required=True,
        description="d",
    )
    assert validate_params([parameter], {"p": given}) == {"p": expected}


def test_a_value_that_will_not_coerce_is_refused_with_the_reason() -> None:
    parameter = Parameter(name="p", type="integer", required=True, description="d")
    with pytest.raises(ParameterError, match="not a valid integer"):
        validate_params([parameter], {"p": "not-a-number"})


def test_params_are_validated_before_the_surface_is_touched(tmp_path: Path) -> None:
    surface = FakeSurface()
    replay, _ = build(surface, tmp_path)
    with pytest.raises(ParameterError):
        replay.run(capability(), {})
    assert surface.actions == [], "nothing may happen before the inputs are known good"


# ---- the happy path ------------------------------------------------------------------------------


def test_a_capability_replays_and_returns_typed_outputs(tmp_path: Path) -> None:
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(), {"member_id": "12345"})

    assert result.status == "success"
    assert result.outputs == {"balance": "4,182.55"}
    assert result.steps_executed == 1
    assert result.locator_usage == {"read-balance": "anchor_relative"}
    assert result.drift_signals == []
    assert result.capability_version == "1.0.0"


def test_a_param_ref_is_substituted_into_the_action(tmp_path: Path) -> None:
    steps = [
        Step(
            id="type-id",
            intent="type the member id",
            action="type",
            target=ControlDescriptor(
                role="textbox",
                name="Member ID",
                candidates=[
                    Locator(
                        strategy="role_name",
                        params={"role": "textbox", "name": "Member ID", "match": "exact"},
                        stability_score=0.9,
                        verified_unique_at_record=True,
                    )
                ],
            ),
            value=ParamRef(param="member_id"),
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        replay.run(capability(steps=steps, outputs=[]), {"member_id": "12345"})

    assert surface.actions[0].value == "12345"


def test_output_types_are_validated_not_merely_copied(tmp_path: Path) -> None:
    outputs = [
        OutputSpec(name="balance", type="number", source_step_id="read-balance", description="d")
    ]
    surface = FakeSurface(extract="  4182.55 ")
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outputs=outputs), {"member_id": "12345"})

    assert result.outputs == {"balance": 4182.55}


def test_an_output_that_will_not_type_is_an_error(tmp_path: Path) -> None:
    outputs = [
        OutputSpec(name="balance", type="integer", source_step_id="read-balance", description="d")
    ]
    surface = FakeSurface(extract="four thousand")
    replay, logger = build(surface, tmp_path)

    with logger, pytest.raises(ParameterError, match="not a valid integer"):
        replay.run(capability(outputs=outputs), {"member_id": "12345"})


def test_a_declared_output_that_was_never_extracted_is_a_failure(tmp_path: Path) -> None:
    steps = [Step(id="read-balance", intent="click", action="click", target=anchor("SAV-88120"))]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(steps=steps), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None and "never extracted" in result.failure.observed


# ---- outcomes: the three-class taxonomy ----------------------------------------------------------


def test_a_business_outcome_is_a_result_not_an_exception(tmp_path: Path) -> None:
    outcomes = [
        Outcome(
            name="member_not_found",
            kind="business",
            detect=Condition(kind="text_present", params={"text": "Member 12345"}),
            message_template="No member matches that id.",
            partial_outputs=["balance"],
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    assert result.status == "business_outcome"
    assert result.outcome is not None
    assert result.outcome.kind == "business"
    assert result.failure is None


def test_a_hard_failure_outcome_stops_with_debuggable_detail(tmp_path: Path) -> None:
    outcomes = [
        Outcome(
            name="session_expired",
            kind="hard_failure",
            detect=Condition(kind="text_present", params={"text": "Member 12345"}),
            message_template="The session expired.",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None
    assert "session_expired" in result.failure.observed


def test_a_recoverable_outcome_retries_and_gives_up_bounded(tmp_path: Path) -> None:
    """The condition never clears, so the retry must terminate rather than spin."""
    outcomes = [
        Outcome(
            name="interstitial",
            kind="recoverable",
            detect=Condition(kind="text_present", params={"text": "Member 12345"}),
            recovery=Recovery(action="wait_and_retry", max_attempts=2),
            message_template="A notice appeared.",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(outcomes=outcomes), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None
    assert "still firing after 2 of 2" in result.failure.observed
    assert len(surface.actions) <= 3, "the retry must be bounded"
    assert result.drift_signals == [
        "read-balance: recovered from 'interstitial' (attempt 1 of 2)",
        "read-balance: recovered from 'interstitial' (attempt 2 of 2)",
    ]


# ---- checkpoints ---------------------------------------------------------------------------------


def test_a_failed_checkpoint_with_no_detector_is_a_hard_failure(tmp_path: Path) -> None:
    steps = [
        Step(
            id="read-balance",
            intent="read the balance",
            action="extract",
            target=anchor("Savings Balance", "cell"),
            checkpoint=Condition(kind="text_present", params={"text": "Nothing like this"}),
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(steps=steps), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None
    assert "text_present" in result.failure.expected


def test_a_met_checkpoint_lets_the_run_continue(tmp_path: Path) -> None:
    steps = [
        Step(
            id="read-balance",
            intent="read the balance",
            action="extract",
            target=anchor("Savings Balance", "cell"),
            checkpoint=Condition(kind="text_present", params={"text": "SAV-88120"}),
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        assert replay.run(capability(steps=steps), {"member_id": "12345"}).status == "success"


# ---- the chokepoint and approval -----------------------------------------------------------------


def test_an_unapproved_capability_is_refused_for_unattended_replay(tmp_path: Path) -> None:
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(approved=False), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None and result.failure.step_id == "<precondition>"
    assert surface.actions == []


def test_an_unapproved_capability_may_still_be_replayed_attended(tmp_path: Path) -> None:
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(approved=False), {"member_id": "12345"}, attended=True)

    assert result.status == "success"


def test_an_irreversible_step_is_blocked_by_the_policy(tmp_path: Path) -> None:
    steps = [
        Step(
            id="read-balance",
            intent="close the account",
            action="click",
            target=anchor("SAV-88120"),
            risk_class="irreversible",
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(steps=steps, outputs=[]), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None and "block_and_escalate" in result.failure.observed
    assert surface.actions == [], "an irreversible step must never reach the surface"


# ---- locator failure -----------------------------------------------------------------------------


def test_an_exhausted_locator_chain_reports_what_it_tried(tmp_path: Path) -> None:
    steps = [
        Step(
            id="read-balance",
            intent="read the balance",
            action="extract",
            target=anchor("ISA-00000", "cell"),
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(steps=steps), {"member_id": "12345"})

    assert result.status == "failure"
    assert result.failure is not None
    assert result.failure.candidates_tried == ["anchor_relative"]
    assert "matched 0" in result.failure.observed


def test_falling_back_to_a_lower_candidate_is_recorded_as_drift(tmp_path: Path) -> None:
    descriptor = ControlDescriptor(
        role="cell",
        name="4,182.55",
        candidates=[
            Locator(
                strategy="anchor_relative",
                params={"anchor_text": "GONE", "relation": "same_row", "target_role": "cell"},
                stability_score=0.95,
                verified_unique_at_record=True,
            ),
            Locator(
                strategy="text_content",
                params={"text": "4,182.55", "match": "exact"},
                stability_score=0.5,
                verified_unique_at_record=True,
            ),
        ],
    )
    steps = [Step(id="read-balance", intent="read", action="extract", target=descriptor)]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        result = replay.run(capability(steps=steps), {"member_id": "12345"})

    assert result.status == "success"
    assert result.locator_usage == {"read-balance": "text_content"}
    assert result.drift_signals and "rank 1" in result.drift_signals[0]


# ---- acting through a fired candidate ------------------------------------------------------------


def test_each_actionable_strategy_becomes_an_action_target() -> None:
    descriptor = anchor("Savings")
    cases: dict[str, dict[str, Any]] = {
        "role_name": {"role": "link", "name": "Open", "match": "exact"},
        "anchor_relative": {
            "anchor_text": "Savings",
            "relation": "same_row",
            "target_role": "link",
            "index": 1,
        },
        "text_content": {"text": "Open", "match": "contains"},
    }
    for strategy, params in cases.items():
        locator = Locator(
            strategy=strategy,  # type: ignore[arg-type]
            params=params,
            stability_score=0.5,
            verified_unique_at_record=True,
        )
        assert can_act_through(locator), strategy
        assert to_action_target(locator, descriptor).role, f"{strategy} produced no role"


@pytest.mark.parametrize(
    ("strategy", "params"),
    [
        ("dom_hint", {"css": "#e123"}),
        ("region_ordinal", {"region": "Sub-accounts", "role": "link", "index": 2}),
        (
            "anchor_relative",
            {"anchor_text": "Savings", "relation": "following", "target_role": "link"},
        ),
        (
            "anchor_relative",
            {"anchor_text": "Sub-accounts", "relation": "within_region", "target_role": "link"},
        ),
    ],
)
def test_strategies_the_surface_cannot_express_are_refused(
    strategy: str, params: dict[str, Any]
) -> None:
    """These resolve against a tree but have no containment equivalent to act through.

    Refusing them keeps the failure honest. Translating them approximately is how a replay
    reads the label instead of the value and reports success.
    """
    locator = Locator(
        strategy=strategy,  # type: ignore[arg-type]
        params=params,
        stability_score=0.3,
        verified_unique_at_record=True,
    )
    assert not can_act_through(locator)
    with pytest.raises(ReplayError, match="cannot act through"):
        to_action_target(locator, anchor("Savings"))


def test_the_engine_skips_unactionable_candidates_by_default(tmp_path: Path) -> None:
    """The engine's default resolver knows what its surface can act through."""
    surface = FakeSurface()
    replay, _ = build(surface, tmp_path)
    assert replay.resolver.actionable is can_act_through


# ---- evidence and loading ------------------------------------------------------------------------


def test_the_run_is_written_to_evidence(tmp_path: Path) -> None:
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        replay.run(capability(), {"member_id": "12345"})

    events = [
        json.loads(line)["event"] for line in logger.path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0] == "replay_start"
    assert "replay_step" in events
    assert events[-1] == "replay_end"


def test_a_sensitive_parameter_never_reaches_the_log(tmp_path: Path) -> None:
    parameters = [
        Parameter(name="member_id", type="string", required=True, description="id"),
        Parameter(
            name="pin", type="string", required=True, sensitive=True, description="verification pin"
        ),
    ]
    steps = [
        Step(
            id="read-balance",
            intent="type the pin",
            action="type",
            target=anchor("Savings Balance", "cell"),
            value=ParamRef(param="pin"),
        )
    ]
    surface = FakeSurface()
    replay, logger = build(surface, tmp_path)

    with logger:
        replay.run(
            capability(parameters=parameters, steps=steps, outputs=[]),
            {"member_id": "12345", "pin": "998877"},
        )

    written = logger.path.read_text(encoding="utf-8")
    assert "998877" not in written
    assert "[REDACTED:pin]" in written or "pin" in written


def test_a_capability_round_trips_through_disk(tmp_path: Path) -> None:
    saved = capability()
    (tmp_path / f"{saved.id}.json").write_text(saved.model_dump_json(), encoding="utf-8")
    assert load_capability(saved.id, tmp_path) == saved


def test_loading_a_missing_capability_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="cannot read capability"):
        load_capability("nope", tmp_path)


def test_a_capability_can_be_loaded_by_path_not_only_by_id(tmp_path: Path) -> None:
    """A discovery run's artifact lands beside its evidence, not in capabilities/.

    Requiring a copy-and-rename before it can be replayed would put a manual step in the
    middle of the one flow this project is about: discover, then replay what you discovered.
    """
    written = capability()
    beside_evidence = tmp_path / "discovery-run" / "capability.json"
    beside_evidence.parent.mkdir(parents=True)
    beside_evidence.write_text(written.model_dump_json(indent=2), encoding="utf-8")

    assert load_capability(str(beside_evidence)).id == written.id


def test_loading_by_id_still_reads_the_capability_directory(tmp_path: Path) -> None:
    written = capability()
    (tmp_path / f"{written.id}.json").write_text(
        written.model_dump_json(indent=2), encoding="utf-8"
    )

    assert load_capability(written.id, tmp_path).id == written.id


def test_a_missing_path_names_the_path_rather_than_a_directory_lookup(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="no-such-run"):
        load_capability(str(tmp_path / "no-such-run" / "capability.json"))


# ---- binding a locator to this invocation's parameters ---------------------------------------


def anchored_on(value: str, **binds: str) -> Locator:
    return Locator(
        strategy="anchor_relative",
        params={"anchor_text": value, "relation": "same_row", "target_role": "cell", "index": 1},
        stability_score=0.9,
        verified_unique_at_record=True,
        binds=binds,
    )


def test_a_bound_param_takes_the_callers_value() -> None:
    """The recorded literal is one member's id; the caller's is the one that matters."""
    locator = anchored_on("12345", anchor_text="member_id")

    assert bind_locator(locator, {"member_id": "67890"}).params["anchor_text"] == "67890"


def test_an_unbound_locator_is_returned_untouched() -> None:
    locator = anchored_on("12345")

    assert bind_locator(locator, {"member_id": "67890"}) is locator


def test_a_bound_param_the_caller_omitted_keeps_the_recorded_literal() -> None:
    """An optional parameter may be absent; resolving against "None" would be worse."""
    locator = anchored_on("12345", anchor_text="member_id")

    assert bind_locator(locator, {}).params["anchor_text"] == "12345"


def test_binding_a_descriptor_binds_every_candidate() -> None:
    descriptor = ControlDescriptor(
        role="cell",
        name="Wilhelmina Okonkwo-Bright",
        candidates=[anchored_on("12345", anchor_text="member_id"), anchored_on("12345")],
    )

    bound = bind_descriptor(descriptor, {"member_id": "67890"})

    assert [c.params["anchor_text"] for c in bound.candidates] == ["67890", "12345"]


def test_binding_does_not_mutate_the_artifact() -> None:
    """A capability is loaded once and replayed many times; binding is per invocation."""
    descriptor = ControlDescriptor(
        role="cell", name="x", candidates=[anchored_on("12345", anchor_text="member_id")]
    )

    bind_descriptor(descriptor, {"member_id": "67890"})

    assert descriptor.candidates[0].params["anchor_text"] == "12345"


def test_the_committed_capability_binds_its_name_cell_to_the_member_id() -> None:
    """The fix for the defect the report named: it replayed for one member and no other."""
    capability = load_capability("member.search", REPO / "capabilities")
    read_name = next(s for s in capability.steps if s.id == "read-name")

    assert read_name.target is not None
    primary = read_name.target.primary
    assert primary.binds == {"anchor_text": "member_id"}, "the primary must not be data-bound"
