"""The artifact schema. If these types are wrong, everything downstream inherits the mistake."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from cua.schema.export import (
    EXPORTED,
    json_schema,
    undescribed_properties,
    undocumented_models,
    write,
)
from cua.schema.models import (
    SURFACE_SPECIFIC_SCORE_CAP,
    AppRef,
    Capability,
    CapabilityOverlay,
    CapabilityPolicy,
    Condition,
    ControlDescriptor,
    EntryPoint,
    FailureDetail,
    Locator,
    Outcome,
    OutcomeResult,
    OutputSpec,
    Override,
    Parameter,
    ParamRef,
    Provenance,
    Recovery,
    ReplayResult,
    StabilityRecord,
    Step,
)


def locator(strategy: str = "role_name", score: float = 0.9, **params: object) -> Locator:
    return Locator(
        strategy=strategy,  # type: ignore[arg-type]
        params=params,
        stability_score=score,
        verified_unique_at_record=True,
    )


def control(role: str = "link", name: str | None = "Open") -> ControlDescriptor:
    return ControlDescriptor(
        role=role,
        name=name,
        candidates=[
            locator("anchor_relative", 0.85, anchor_text="Savings", relation="same_row"),
            locator("role_name", 0.6, role=role, name=name, match="exact"),
            locator("dom_hint", 0.9, css="#row-3 > td:nth-child(4) > a"),
        ],
    )


@pytest.fixture
def capability() -> Capability:
    """A capability exercising every model in the schema."""
    return Capability(
        id="member.savings_balance.lookup",
        name="Look up savings balance",
        description="Find a member by id and read the current balance of their savings account.",
        version="1.0.0",
        app=AppRef(vendor_product="meridian-core", product_version="4.2.1", tenant_id=None),
        entry=EntryPoint(url="http://127.0.0.1:8099/tenant-a/"),
        parameters=[
            Parameter(
                name="member_id",
                type="string",
                required=True,
                description="The member's identifier as printed on their statement.",
                example="12345",
            ),
            Parameter(
                name="verification_pin",
                type="string",
                required=False,
                sensitive=True,
                description="Caller-supplied verification pin.",
                example="9876",
            ),
        ],
        outputs=[
            OutputSpec(
                name="balance",
                type="string",
                source_step_id="read-balance",
                description="Current balance of the savings sub-account.",
            )
        ],
        steps=[
            Step(
                id="enter-member-id",
                intent="type the member id into the search box",
                action="type",
                target=control("textbox", "Member ID"),
                value=ParamRef(param="member_id"),
                checkpoint=Condition(kind="control_present", params={"role": "link"}),
                risk_class="safe",
            ),
            Step(
                id="open-savings",
                intent="open the savings sub-account",
                action="click",
                target=control(),
                risk_class="safe",
            ),
            Step(
                id="read-balance",
                intent="read the current balance",
                action="extract",
                target=control("cell", None),
                checkpoint=Condition(kind="text_present", params={"text": "Savings Balance"}),
            ),
        ],
        outcomes=[
            Outcome(
                name="member_not_found",
                kind="business",
                detect=Condition(kind="text_present", params={"text": "No member matches"}),
                applies_to=["enter-member-id"],
                message_template="No member matches {member_id}.",
            ),
            Outcome(
                name="maintenance_interstitial",
                kind="recoverable",
                detect=Condition(kind="control_present", params={"role": "dialog"}),
                recovery=Recovery(
                    action="dismiss_dialog", max_attempts=2, target=control("link", "Acknowledge")
                ),
                message_template="Dismissed a maintenance notice.",
            ),
            Outcome(
                name="session_expired",
                kind="hard_failure",
                detect=Condition(kind="url_matches", params={"pattern": ".*session-expired"}),
                message_template="The session expired mid-flow.",
                partial_outputs=["balance"],
            ),
        ],
        policy=CapabilityPolicy(allowed_hosts=["127.0.0.1:*"], max_steps=10),
        provenance=Provenance(
            discovered_at=datetime(2026, 9, 10, 11, 8, tzinfo=UTC),
            model="openai/gpt-oss-120b",
            discovery_run_id="discovery-20260910T110803-8bd5f2",
            state="approved",
            approved_by="aditi",
            outcomes_reviewed=True,
        ),
        stability=StabilityRecord(
            runs=10,
            passes=10,
            measured_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
            locator_usage={"open-savings": {"anchor_relative": 10}},
        ),
    )


# ---- round trip --------------------------------------------------------------------------


def test_a_capability_survives_a_json_round_trip(capability: Capability) -> None:
    """Serialise, write, read, deserialise, and get the same object back."""
    encoded = json.dumps(capability.model_dump(mode="json"))
    restored = Capability.model_validate(json.loads(encoded))
    assert restored == capability


def test_a_round_tripped_capability_validates_against_the_exported_schema(
    capability: Capability,
) -> None:
    """The contract we publish must accept the artifacts we produce."""
    Draft202012Validator.check_schema(json_schema(Capability))
    Draft202012Validator(json_schema(Capability)).validate(capability.model_dump(mode="json"))


@pytest.mark.parametrize("name", sorted(EXPORTED))
def test_every_exported_schema_is_valid_json_schema(name: str) -> None:
    Draft202012Validator.check_schema(json_schema(EXPORTED[name]))


def test_the_schema_rejects_an_artifact_missing_a_required_field(capability: Capability) -> None:
    broken = capability.model_dump(mode="json")
    del broken["provenance"]
    validator = Draft202012Validator(json_schema(Capability))
    assert not validator.is_valid(broken)


def test_replay_result_round_trips(capability: Capability) -> None:
    result = ReplayResult(
        status="business_outcome",
        capability_id=capability.id,
        capability_version=capability.version,
        run_id="replay-1",
        outcome=OutcomeResult(name="member_not_found", kind="business", message="No member."),
        steps_executed=1,
        duration_ms=812,
        locator_usage={"enter-member-id": "role_name"},
    )
    assert ReplayResult.model_validate(json.loads(result.model_dump_json())) == result
    Draft202012Validator(json_schema(ReplayResult)).validate(result.model_dump(mode="json"))


def test_an_overlay_round_trips() -> None:
    overlay = CapabilityOverlay(
        base_capability_id="member.savings_balance.lookup",
        base_version="1.0.0",
        tenant_id="tenant-b",
        overrides=[
            Override(
                step_id="read-balance",
                field_path="checkpoint.params.text",
                value="Deposit Balance",
            )
        ],
        verified_against="1.0.0",
    )
    assert CapabilityOverlay.model_validate(json.loads(overlay.model_dump_json())) == overlay
    assert not overlay.needs_review("1.0.0")
    assert overlay.needs_review("1.1.0")


# ---- the export is the contract, so it must be described ---------------------------------


@pytest.mark.parametrize("name", sorted(EXPORTED))
def test_every_property_in_the_contract_has_a_description(name: str) -> None:
    holes = undescribed_properties(json_schema(EXPORTED[name]))
    assert holes == [], f"{name} has undescribed properties: {holes}"


@pytest.mark.parametrize("name", sorted(EXPORTED))
def test_every_model_in_the_contract_is_documented(name: str) -> None:
    missing = undocumented_models(json_schema(EXPORTED[name]))
    assert missing == [], f"{name} has undocumented models: {missing}"


def test_the_checker_would_notice_a_missing_description() -> None:
    """A contract check that cannot fail is not a check."""
    assert undescribed_properties({"title": "T", "properties": {"x": {"type": "string"}}}) == [
        "T.x"
    ]


def test_schemas_are_written_to_disk(tmp_path: Path) -> None:
    paths = write(tmp_path)
    assert {p.name for p in paths} == {f"{n}.schema.json" for n in EXPORTED}
    loaded = json.loads(paths[0].read_text(encoding="utf-8"))
    assert loaded["$schema"].startswith("https://json-schema.org/")


# ---- invariants ----------------------------------------------------------------------------


def test_dom_hint_is_always_surface_specific_and_capped() -> None:
    """A css selector cannot pass itself off as a portable, high-confidence candidate (C3)."""
    hint = Locator(
        strategy="dom_hint",
        params={"css": "#x"},
        stability_score=0.99,
        verified_unique_at_record=True,
        surface_specific=False,
    )
    assert hint.surface_specific is True
    assert hint.stability_score == SURFACE_SPECIFIC_SCORE_CAP


def test_nothing_but_dom_hint_is_surface_specific() -> None:
    portable = Locator(
        strategy="anchor_relative",
        params={"anchor_text": "Savings"},
        stability_score=0.8,
        verified_unique_at_record=True,
        surface_specific=True,
    )
    assert portable.surface_specific is False
    assert portable.stability_score == 0.8


def test_candidates_are_ranked_not_merely_stored() -> None:
    descriptor = control()
    scores = [c.stability_score for c in descriptor.candidates]
    assert scores == sorted(scores, reverse=True)
    assert descriptor.primary.strategy == "anchor_relative"


def test_a_control_must_offer_at_least_one_candidate() -> None:
    with pytest.raises(ValidationError):
        ControlDescriptor(role="button", name="Search", candidates=[])


def test_portable_candidates_exclude_the_dom_hint() -> None:
    descriptor = control()
    assert len(descriptor.candidates) == 3
    assert [c.strategy for c in descriptor.portable_candidates] == ["anchor_relative", "role_name"]


def test_only_a_recoverable_outcome_may_carry_a_recovery() -> None:
    with pytest.raises(ValidationError, match="cannot carry a recovery"):
        Outcome(
            name="member_not_found",
            kind="business",
            detect=Condition(kind="text_present", params={"text": "none"}),
            recovery=Recovery(action="retry_step", max_attempts=1),
            message_template="No member.",
        )


def test_a_recoverable_outcome_must_say_how_to_recover() -> None:
    with pytest.raises(ValidationError, match="declares no recovery"):
        Outcome(
            name="interstitial",
            kind="recoverable",
            detect=Condition(kind="control_present", params={"role": "dialog"}),
            message_template="A dialog appeared.",
        )


def test_a_sensitive_parameter_cannot_carry_an_example() -> None:
    """An example of a real account number is a real account number."""
    parameter = Parameter(
        name="pin",
        type="string",
        required=True,
        sensitive=True,
        description="Verification pin.",
        example="4321",
    )
    assert parameter.example is None


def test_a_non_sensitive_parameter_keeps_its_example() -> None:
    parameter = Parameter(
        name="member_id",
        type="string",
        required=True,
        description="Member id.",
        example="12345",
    )
    assert parameter.example == "12345"


def test_sensitive_parameters_are_listed_for_the_redaction_filter(capability: Capability) -> None:
    assert capability.sensitive_parameters == ["verification_pin"]


def test_a_sensitive_value_never_appears_in_the_serialised_artifact(
    capability: Capability,
) -> None:
    """The artifact records the reference to a sensitive value, never the value."""
    encoded = capability.model_dump_json()
    assert "verification_pin" in encoded  # the declaration is fine
    assert "9876" not in encoded  # the example value is not


def test_unattended_replay_needs_both_gates() -> None:
    approved_unreviewed = Provenance(
        discovered_at=datetime.now(UTC),
        model="m",
        discovery_run_id="r",
        state="approved",
        outcomes_reviewed=False,
    )
    assert not approved_unreviewed.replayable_unattended

    reviewed_draft = approved_unreviewed.model_copy(
        update={"state": "draft", "outcomes_reviewed": True}
    )
    assert not reviewed_draft.replayable_unattended

    both = approved_unreviewed.model_copy(update={"outcomes_reviewed": True})
    assert both.replayable_unattended


def test_step_ids_must_be_unique(capability: Capability) -> None:
    data = capability.model_dump(mode="json")
    data["steps"][1]["id"] = data["steps"][0]["id"]
    with pytest.raises(ValidationError, match="step ids must be unique"):
        Capability.model_validate(data)


def test_a_step_cannot_reference_an_undeclared_parameter(capability: Capability) -> None:
    data = capability.model_dump(mode="json")
    data["steps"][0]["value"] = {"param": "nope"}
    with pytest.raises(ValidationError, match="unknown parameter"):
        Capability.model_validate(data)


def test_an_output_cannot_name_a_missing_step(capability: Capability) -> None:
    data = capability.model_dump(mode="json")
    data["outputs"][0]["source_step_id"] = "ghost-step"
    with pytest.raises(ValidationError, match="unknown source step"):
        Capability.model_validate(data)


def test_an_outcome_cannot_name_a_missing_step(capability: Capability) -> None:
    data = capability.model_dump(mode="json")
    data["outcomes"][0]["applies_to"] = ["ghost-step"]
    with pytest.raises(ValidationError, match="unknown steps"):
        Capability.model_validate(data)


def test_an_outcome_cannot_promise_an_undeclared_output(capability: Capability) -> None:
    data = capability.model_dump(mode="json")
    data["outcomes"][2]["partial_outputs"] = ["not_an_output"]
    with pytest.raises(ValidationError, match="unknown outputs"):
        Capability.model_validate(data)


def test_a_capability_needs_at_least_one_step(capability: Capability) -> None:
    data = capability.model_dump(mode="json")
    data["steps"] = []
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_a_failure_result_must_be_debuggable() -> None:
    with pytest.raises(ValidationError, match="must carry failure detail"):
        ReplayResult(
            status="failure",
            capability_id="c",
            capability_version="1.0.0",
            run_id="r",
            steps_executed=2,
            duration_ms=10,
        )


def test_a_business_outcome_result_must_name_the_outcome() -> None:
    with pytest.raises(ValidationError, match="must name the outcome"):
        ReplayResult(
            status="business_outcome",
            capability_id="c",
            capability_version="1.0.0",
            run_id="r",
            steps_executed=1,
            duration_ms=10,
        )


def test_a_failure_result_carries_the_candidates_it_tried() -> None:
    result = ReplayResult(
        status="failure",
        capability_id="c",
        capability_version="1.0.0",
        run_id="r",
        failure=FailureDetail(
            step_id="open-savings",
            expected="a link named Open near Savings",
            observed="no matching control",
            candidates_tried=["anchor_relative", "role_name", "dom_hint"],
            evidence_paths=["evidence/replay-1/step-02.png"],
        ),
        steps_executed=2,
        duration_ms=1200,
    )
    assert result.failure is not None
    assert result.failure.candidates_tried[-1] == "dom_hint"


def test_pass_rate_handles_a_record_with_no_runs() -> None:
    empty = StabilityRecord(runs=0, passes=0, measured_at=datetime.now(UTC))
    assert empty.pass_rate == 0.0
    assert StabilityRecord(runs=10, passes=9, measured_at=datetime.now(UTC)).pass_rate == 0.9
