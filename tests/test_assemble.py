"""Assembling a draft capability from a discovery trace.

The thing to get right is what the draft does *not* claim. It is the model's choice of
controls, unreviewed and unverified, and every field that could imply otherwise has to say so:
draft state, no outcomes, unverified candidates, and no output pointing at a step that did not
produce it.
"""

import pytest

from cua.recording.assemble import ActedStep, assemble, slug, step_id
from cua.recording.synthesizer import SynthesisError
from cua.schema.models import Capability
from cua.surfaces.base import Action, ActionKind, ActionTarget, AXNode

SCREEN = AXNode(
    role="RootWebArea",
    name="Member search",
    children=[
        AXNode(role="textbox", name="Member ID"),
        AXNode(role="link", name="Search"),
        AXNode(role="cell", name="Wilhelmina Okonkwo-Bright"),
    ],
)


def acted(
    kind: ActionKind,
    role: str,
    name: str,
    *,
    value: str | None = None,
    extracted: str | None = None,
) -> ActedStep:
    return ActedStep(
        tree=SCREEN,
        action=Action(kind=kind, target=ActionTarget(role=role, name=name), value=value),
        extracted=extracted,
    )


def build(*steps: ActedStep, outputs: dict[str, str] | None = None) -> Capability:
    return assemble(
        goal="Look up a member and read their name",
        run_id="discovery-1",
        model="test-model",
        entry_url="http://127.0.0.1:8099/tenant-a/",
        acted=list(steps),
        tenant_id="tenant-a",
        outputs=outputs,
    )


# ---- the shape of the draft --------------------------------------------------------------


def test_each_acted_step_becomes_a_step_with_a_candidate_chain() -> None:
    capability = build(acted("type", "textbox", "Member ID", value="12345"))

    step = capability.steps[0]
    assert step.action == "type"
    assert step.value == "12345"
    assert step.target is not None
    assert len(step.target.candidates) > 1, "a control is a chain, never one locator"


def test_the_entry_navigate_is_not_repeated_as_a_step() -> None:
    """The entry point lives on the capability. A step for it would replay it twice."""
    entry = ActedStep(tree=SCREEN, action=Action(kind="navigate", value="http://x/"))

    capability = build(entry, acted("click", "link", "Search"))

    assert [s.action for s in capability.steps] == ["click"]
    assert capability.entry.url == "http://127.0.0.1:8099/tenant-a/"


def test_step_ids_are_readable_and_ordered() -> None:
    capability = build(acted("type", "textbox", "Member ID"), acted("click", "link", "Search"))

    assert [s.id for s in capability.steps] == ["01-member-id", "02-search"]


def test_a_trace_that_acted_on_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="acted on nothing"):
        build()


def test_an_action_whose_control_is_absent_from_its_own_tree_raises() -> None:
    """The trace and the tree have to agree; a draft built from a mismatch is fiction."""
    ghost = ActedStep(
        tree=SCREEN, action=Action(kind="click", target=ActionTarget(role="button", name="Nope"))
    )

    with pytest.raises(SynthesisError):
        build(ghost)


# ---- what the draft refuses to claim -------------------------------------------------------


def test_the_draft_cannot_replay_unattended() -> None:
    capability = build(acted("click", "link", "Search"))

    assert capability.provenance.state == "draft"
    assert capability.provenance.outcomes_reviewed is False
    assert capability.provenance.replayable_unattended is False


def test_the_draft_declares_no_outcomes() -> None:
    """One successful run has nothing to say about error screens it never saw."""
    assert build(acted("click", "link", "Search")).outcomes == []


def test_every_candidate_says_it_was_never_verified() -> None:
    """The trace is over, so nothing could be re-resolved. The flag has to admit that."""
    capability = build(acted("click", "link", "Search"))

    target = capability.steps[0].target
    assert target is not None
    assert all(not c.verified_unique_at_record for c in target.candidates)


def test_the_draft_records_which_run_and_model_produced_it() -> None:
    capability = build(acted("click", "link", "Search"))

    assert capability.provenance.discovery_run_id == "discovery-1"
    assert capability.provenance.model == "test-model"
    assert capability.app.tenant_id == "tenant-a"


def test_typed_values_stay_literals_rather_than_becoming_guessed_parameters() -> None:
    """Deciding a value is a parameter is a judgement about intent. Guessing it is worse."""
    capability = build(acted("type", "textbox", "Member ID", value="12345"))

    assert capability.parameters == []
    assert capability.steps[0].value == "12345"


# ---- outputs -------------------------------------------------------------------------------


def test_an_output_is_wired_to_the_step_that_extracted_its_value() -> None:
    capability = build(
        acted("click", "link", "Search"),
        acted(
            "extract", "cell", "Wilhelmina Okonkwo-Bright", extracted="Wilhelmina Okonkwo-Bright"
        ),
        outputs={"member name": "Wilhelmina Okonkwo-Bright"},
    )

    assert [o.name for o in capability.outputs] == ["member_name"]
    assert capability.outputs[0].source_step_id == "02-wilhelmina-okonkwo-brigh"


def test_an_output_no_step_extracted_is_dropped_not_invented() -> None:
    """Pointing it at an arbitrary step would produce a capability that cannot deliver it."""
    capability = build(acted("click", "link", "Search"), outputs={"balance": "4,182.55"})

    assert capability.outputs == []


# ---- helpers -------------------------------------------------------------------------------


def test_slug_survives_punctuation_and_case() -> None:
    assert slug("Look up member 12345!") == "look-up-member-12345"


def test_slug_never_returns_empty() -> None:
    assert slug("!!!") == "unnamed"


def test_step_id_falls_back_to_the_action_when_there_is_no_name() -> None:
    assert step_id(3, Action(kind="press_key", value="Enter")) == "03-press-key"
