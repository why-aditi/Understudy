"""The catalog: what an agent is shown, and what happens when it calls.

Two things matter here. The tool declarations have to be honest - the types, the required
list and the refusal of unknown arguments must match what validation actually does, because
a model reads the schema and believes it. And the approval gate has to hold: an agent calling
a tool unsupervised is the unattended case, so a draft is not callable.
"""

from pathlib import Path
from typing import Any

import pytest

from cua.catalog.catalog import (
    Catalog,
    CatalogError,
    NotApproved,
    UnknownTool,
    discover,
    input_schema,
    output_schema,
    to_tool,
)
from cua.schema.models import Capability, OutputSpec, Parameter, ReplayResult, Step
from test_replay_engine import anchor, capability

REPO = Path(__file__).resolve().parents[1]


def searchable(*, approved: bool = True, parameters: list[Parameter] | None = None) -> Capability:
    """A one-step capability with a member_id parameter and a member_name output."""
    return capability(
        parameters=parameters
        if parameters is not None
        else [
            Parameter(
                name="member_id",
                type="string",
                required=True,
                description="The member's id.",
                example="12345",
            )
        ],
        steps=[
            Step(
                id="read-name",
                intent="read the name",
                action="extract",
                target=anchor("Member", "cell"),
            )
        ],
        outputs=[
            OutputSpec(
                name="member_name",
                type="string",
                source_step_id="read-name",
                description="The member's full name.",
            )
        ],
        approved=approved,
    )


def succeeding() -> tuple[list[dict[str, Any]], Any]:
    """An invoke that records what it was handed and returns a successful result."""
    seen: list[dict[str, Any]] = []

    def invoke(cap: Capability, params: dict[str, Any]) -> ReplayResult:
        seen.append({"capability": cap, "params": params})
        return ReplayResult(
            status="success",
            capability_id=cap.id,
            capability_version=cap.version,
            run_id="r",
            outputs={"member_name": "Wilhelmina Okonkwo-Bright"},
            steps_executed=1,
            duration_ms=10,
        )

    return seen, invoke


# ---- the tool declaration -------------------------------------------------------------------


def test_arguments_are_typed_from_the_declared_parameters() -> None:
    schema = input_schema(searchable())

    assert schema["type"] == "object"
    assert schema["properties"]["member_id"]["type"] == "string"
    assert schema["properties"]["member_id"]["description"] == "The member's id."
    assert schema["required"] == ["member_id"]


def test_an_example_reaches_the_schema_so_a_model_sees_the_shape() -> None:
    assert input_schema(searchable())["properties"]["member_id"]["examples"] == ["12345"]


def test_unknown_arguments_are_refused_by_the_schema_as_well_as_by_validation() -> None:
    """The schema must not promise something laxer than the code enforces."""
    assert input_schema(searchable())["additionalProperties"] is False


def test_an_optional_parameter_is_not_required() -> None:
    parameters = [
        Parameter(name="member_id", type="string", required=True, description="d"),
        Parameter(name="as_of", type="date", required=False, description="d"),
    ]
    schema = input_schema(searchable(parameters=parameters))

    assert schema["required"] == ["member_id"]
    assert "as_of" in schema["properties"]


def test_a_date_becomes_a_formatted_string_because_json_schema_has_no_date() -> None:
    parameters = [Parameter(name="as_of", type="date", required=True, description="d")]

    prop = input_schema(searchable(parameters=parameters))["properties"]["as_of"]

    assert prop == {"type": "string", "format": "date", "description": "d"}


def test_a_sensitive_parameter_is_marked_and_carries_no_example() -> None:
    """An example of a real account number is a real account number."""
    parameters = [
        Parameter(
            name="account",
            type="string",
            required=True,
            sensitive=True,
            description="Account number.",
            example="4111111111111111",
        )
    ]

    prop = input_schema(searchable(parameters=parameters))["properties"]["account"]

    assert prop["writeOnly"] is True
    assert "never persisted" in prop["description"]
    assert "examples" not in prop


def test_a_capability_with_no_parameters_still_produces_a_usable_schema() -> None:
    schema = input_schema(searchable(parameters=[]))

    assert schema["properties"] == {}
    assert schema["required"] == []


# ---- the result declaration --------------------------------------------------------------------


def test_outputs_are_typed_from_the_declared_output_specs() -> None:
    outputs = output_schema(searchable())["properties"]["outputs"]["anyOf"][0]

    assert outputs["properties"]["member_name"]["type"] == "string"
    assert outputs["properties"]["member_name"]["description"] == "The member's full name."
    assert outputs["required"] == ["member_name"]


def test_the_result_schema_describes_the_envelope_not_just_the_payload() -> None:
    """A business outcome is an answer, and an agent has to be told how to recognise one."""
    schema = output_schema(searchable())

    assert schema["properties"]["status"]["enum"] == ["success", "business_outcome", "failure"]
    assert "outcome" in schema["properties"]
    assert "failure" in schema["properties"]
    assert "not an error" in schema["description"]


def test_the_result_schema_points_at_the_exported_contract() -> None:
    reference = output_schema(searchable())["$comment"]

    assert "replay-result.schema.json" in reference
    assert (REPO / reference.split(": ")[1]).exists(), "the schema it names must be on disk"


def test_a_tool_names_the_artifact_behind_it() -> None:
    tool = to_tool(searchable())

    assert tool.name == searchable().id
    assert tool.capability_version == "1.0.0"
    assert tool.description == searchable().description


# ---- discovery and the approval gate ------------------------------------------------------------


def test_only_approved_capabilities_are_listed() -> None:
    _seen, invoke = succeeding()
    catalog = Catalog([searchable(approved=False)], invoke)

    assert catalog.tools() == []


def test_an_approved_capability_is_listed() -> None:
    _seen, invoke = succeeding()

    assert [tool.name for tool in Catalog([searchable()], invoke).tools()] == [searchable().id]


def test_calling_a_draft_says_what_is_missing_rather_than_pretending_it_is_absent() -> None:
    seen, invoke = succeeding()
    catalog = Catalog([searchable(approved=False)], invoke)

    with pytest.raises(NotApproved, match="state=draft"):
        catalog.call(searchable().id, {"member_id": "12345"})
    assert seen == [], "nothing ran"


def test_calling_an_unknown_tool_is_a_different_error_from_an_unapproved_one() -> None:
    _seen, invoke = succeeding()

    with pytest.raises(UnknownTool, match="no capability named"):
        Catalog([searchable()], invoke).call("member.teleport", {})


def test_describe_refuses_a_draft_too() -> None:
    _seen, invoke = succeeding()

    with pytest.raises(NotApproved):
        Catalog([searchable(approved=False)], invoke).describe(searchable().id)


def test_discovery_skips_files_that_are_not_capabilities(tmp_path: Path) -> None:
    """The directory also holds outcome proposals and exported schemas."""
    (tmp_path / "real.json").write_text(searchable().model_dump_json(), encoding="utf-8")
    (tmp_path / "notes.json").write_text('{"not": "a capability"}', encoding="utf-8")
    (tmp_path / "broken.json").write_text("{{{", encoding="utf-8")

    found = discover(tmp_path)

    assert [c.id for c in found] == [searchable().id]


def test_discovery_of_an_empty_directory_is_an_empty_catalog(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


# ---- calling ---------------------------------------------------------------------------------


def test_a_call_reaches_replay_with_the_validated_arguments() -> None:
    seen, invoke = succeeding()
    catalog = Catalog([searchable()], invoke)

    result = catalog.call(searchable().id, {"member_id": "12345"})

    assert seen[0]["params"] == {"member_id": "12345"}
    assert result.status == "success"
    assert result.outputs == {"member_name": "Wilhelmina Okonkwo-Bright"}


def test_arguments_are_coerced_to_their_declared_type_before_anything_runs() -> None:
    """A model that emits a number where the artifact declared a string is corrected here."""
    seen, invoke = succeeding()
    catalog = Catalog([searchable()], invoke)

    catalog.call(searchable().id, {"member_id": 12345})

    assert seen[0]["params"] == {"member_id": "12345"}


def test_a_missing_required_argument_is_refused_before_replay() -> None:
    seen, invoke = succeeding()

    with pytest.raises(CatalogError, match="missing"):
        Catalog([searchable()], invoke).call(searchable().id, {})
    assert seen == []


def test_an_unknown_argument_is_refused_before_replay() -> None:
    """A model's typo should cost nothing, and should not silently run on a default."""
    seen, invoke = succeeding()

    with pytest.raises(CatalogError, match="unknown parameter"):
        Catalog([searchable()], invoke).call(searchable().id, {"membre_id": "12345"})
    assert seen == []


def test_a_business_outcome_comes_back_as_a_result_not_an_exception() -> None:
    """The whole point of the envelope: "no such member" is an answer."""

    def invoke(cap: Capability, params: dict[str, Any]) -> ReplayResult:
        return ReplayResult(
            status="business_outcome",
            capability_id=cap.id,
            capability_version=cap.version,
            run_id="r",
            outcome={  # type: ignore[arg-type]
                "name": "member_not_found",
                "kind": "business",
                "message": "No member matches that id.",
            },
            steps_executed=1,
            duration_ms=10,
        )

    result = Catalog([searchable()], invoke).call(searchable().id, {"member_id": "99999"})

    assert result.status == "business_outcome"
    assert result.outcome is not None
    assert result.outcome.name == "member_not_found"


# ---- the catalog over the committed artifacts ------------------------------------------------


def test_the_committed_capabilities_produce_a_usable_tool_list() -> None:
    """What an agent would actually be handed, checked in CI rather than only in a demo."""
    _seen, invoke = succeeding()
    catalog = Catalog.from_directory(invoke, REPO / "capabilities")

    tools = catalog.tools()

    assert tools, "the repo ships at least one approved capability"
    for tool in tools:
        assert tool.description, "a tool a model cannot read the purpose of is not callable"
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema["properties"]["status"]["enum"]


def test_every_listed_tool_is_approved() -> None:
    _seen, invoke = succeeding()
    catalog = Catalog.from_directory(invoke, REPO / "capabilities")

    for tool in catalog.tools():
        assert catalog.describe(tool.name).name == tool.name, "listing implies callability"


def test_the_catalog_imports_no_provider() -> None:
    """The catalog is agent-facing, not provider-facing: assembling that is a caller's job."""
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cua.catalog.catalog, sys; "
            "print([m for m in sys.modules if m.startswith('cua.llm')])",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    assert completed.stdout.strip() == "[]", f"provider loaded: {completed.stdout.strip()}"
