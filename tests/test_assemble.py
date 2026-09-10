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
NAME = "Wilhelmina Okonkwo-Bright"


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


# ---- the descriptor has to describe what actually happened ---------------------------------


def test_an_extract_whose_descriptor_disagrees_with_what_was_read_is_dropped() -> None:
    """The synthesizer resolves against the tree; the surface resolved against the live page.

    When those disagreed, the draft recorded a descriptor for a control the run never
    touched and nothing said so. Only an extract carries ground truth, so only an extract
    can be checked - and it is.
    """
    wrong = ActedStep(
        tree=SCREEN,
        action=Action(kind="extract", target=ActionTarget(role="cell", name=NAME)),
        extracted="Member search",
    )

    capability = build(acted("click", "link", "Search"), wrong)

    assert [s.action for s in capability.steps] == ["click"], "the mismatched read is not kept"


def test_an_extract_that_matches_what_was_read_is_kept() -> None:
    right = ActedStep(
        tree=SCREEN,
        action=Action(kind="extract", target=ActionTarget(role="cell", name=NAME)),
        extracted=NAME,
    )

    capability = build(right)

    assert [s.action for s in capability.steps] == ["extract"]


def test_whitespace_differences_are_not_a_mismatch() -> None:
    """`inner_text` and an accessible name differ in spacing, not in meaning."""
    padded = ActedStep(
        tree=SCREEN,
        action=Action(kind="extract", target=ActionTarget(role="cell", name=NAME)),
        extracted=f"  {NAME}\n ",
    )

    assert len(build(padded).steps) == 1


def test_an_extract_resolving_to_nothing_is_dropped_rather_than_raising() -> None:
    """A trace can outlive the screen it was taken from; that is a skip, not a crash."""
    ghost = ActedStep(
        tree=SCREEN,
        action=Action(kind="extract", target=ActionTarget(role="cell", name="Absent")),
        extracted="something",
    )

    capability = build(acted("click", "link", "Search"), ghost)

    assert [s.action for s in capability.steps] == ["click"]


# ---- the model repeating itself ------------------------------------------------------------


def test_the_same_read_twice_in_a_row_becomes_one_step() -> None:
    """A read has no side effect, so two in a row is the model repeating itself."""
    read = ActedStep(
        tree=SCREEN,
        action=Action(kind="extract", target=ActionTarget(role="cell", name=NAME)),
        extracted=NAME,
    )

    capability = build(read, read, read, outputs={"member name": NAME})

    assert len(capability.steps) == 1
    assert [o.source_step_id for o in capability.outputs] == [capability.steps[0].id]


def test_two_clicks_in_a_row_are_not_collapsed() -> None:
    """Clicking the same control twice can be two real things. Only reads are provably not."""
    click = acted("click", "link", "Search")

    assert len(build(click, click).steps) == 2


def test_a_read_repeated_after_something_else_is_kept() -> None:
    """Only *consecutive* reads collapse: reading, acting, then reading again is a real flow."""
    read = ActedStep(
        tree=SCREEN,
        action=Action(kind="extract", target=ActionTarget(role="cell", name=NAME)),
        extracted=NAME,
    )

    capability = build(read, acted("click", "link", "Search"), read)

    assert [s.action for s in capability.steps] == ["extract", "click", "extract"]


# ---- the product it was recorded against ---------------------------------------------------


def test_the_vendor_product_is_recorded_when_supplied() -> None:
    capability = assemble(
        goal="g",
        run_id="r",
        model="m",
        entry_url="http://x/",
        acted=[acted("click", "link", "Search")],
        vendor_product="meridian-core",
    )

    assert capability.app.vendor_product == "meridian-core"


def test_the_vendor_product_defaults_to_something_honest() -> None:
    """Nothing on a page reliably says which product it is, so it is not guessed."""
    assert build(acted("click", "link", "Search")).app.vendor_product == "unknown"
