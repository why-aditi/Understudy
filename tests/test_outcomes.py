"""The outcome-proposal pass and the human gate that makes it safe to use.

The pass is a guess generator. These tests are mostly about what happens to a bad guess.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cua.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from cua.recording.outcomes import (
    PROPOSE_TOOL,
    OutcomeProposal,
    ProposalError,
    apply_review,
    approve,
    describe_proposal,
    load_proposals,
    propose,
    render_flow,
    save_proposals,
)
from cua.schema.models import (
    AppRef,
    Capability,
    Condition,
    EntryPoint,
    Outcome,
    Provenance,
    Step,
)


def capability(*, reviewed: bool = False, outcomes: list[Outcome] | None = None) -> Capability:
    return Capability(
        id="member.savings_balance.lookup",
        name="Look up savings balance",
        description="Find a member by id and read their savings balance.",
        version="1.0.0",
        app=AppRef(vendor_product="meridian-core", tenant_id="tenant-a"),
        entry=EntryPoint(url="http://127.0.0.1:8099/tenant-a/"),
        steps=[
            Step(id="search", intent="search for the member", action="type"),
            Step(
                id="open-savings",
                intent="open the savings sub-account",
                action="click",
                checkpoint=Condition(kind="text_present", params={"text": "Savings Balance"}),
            ),
        ],
        outcomes=outcomes or [],
        provenance=Provenance(
            discovered_at=datetime(2026, 9, 10, tzinfo=UTC),
            model="fake",
            discovery_run_id="r",
            outcomes_reviewed=reviewed,
        ),
    )


class FakeLLM:
    """Replies with whatever proposals the test scripts."""

    supports_images = False

    def __init__(self, outcomes: list[dict[str, Any]] | None = None, *, text: str = "") -> None:
        self._outcomes = outcomes
        self._text = text
        self.messages: list[list[Message]] = []
        self.images_seen: list[list[bytes]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        images: list[bytes] | None = None,
    ) -> LLMResponse:
        self.messages.append(messages)
        if images is not None:
            self.images_seen.append(images)
        if self._outcomes is None:
            return LLMResponse(model="fake", text=self._text, tool_calls=[])
        return LLMResponse(
            model="fake",
            text=None,
            tool_calls=[ToolCall(name="propose_outcomes", arguments={"outcomes": self._outcomes})],
        )


GOOD = {
    "name": "member_not_found",
    "kind": "business",
    "step_id": "search",
    "detect_kind": "text_present",
    "detect_params": {"text": "No member matches"},
    "message_template": "No member matches {member_id}.",
    "rationale": "Search screens usually re-render with a message.",
}
RECOVERABLE = {
    "name": "maintenance_notice",
    "kind": "recoverable",
    "step_id": "",
    "detect_kind": "control_present",
    "detect_params": {"role": "dialog", "name": "Service notice"},
    "message_template": "Dismissed a maintenance notice.",
    "rationale": "Banking apps often show scheduled-maintenance interstitials.",
}


# ---- the pass is text-only -------------------------------------------------------------


def test_the_flow_is_rendered_as_text_with_no_images() -> None:
    rendered = render_flow(capability())
    assert "[search] search for the member" in rendered
    assert "Savings Balance" in rendered
    assert "meridian-core" in rendered


def test_the_pass_sends_no_images(_: None = None) -> None:
    llm = FakeLLM([GOOD])
    propose(capability(), llm)
    assert llm.images_seen == [], "the proposal pass never needs pixels"


def test_the_pass_is_given_the_taxonomy_it_must_classify_into() -> None:
    llm = FakeLLM([GOOD])
    propose(capability(), llm)
    system = llm.messages[0][0].content
    for kind in ("business", "recoverable", "hard_failure"):
        assert kind in system


def test_the_tool_schema_asks_for_the_four_required_fields() -> None:
    properties = PROPOSE_TOOL.parameters["properties"]["outcomes"]["items"]["properties"]
    assert {"name", "kind", "detect_kind", "message_template"} <= set(properties)


# ---- what happens to a bad guess ---------------------------------------------------------


def test_a_proposal_with_an_invented_kind_is_dropped() -> None:
    """The three-class taxonomy is closed. A fourth kind is not a proposal, it is noise."""
    bad = GOOD | {"kind": "catastrophic", "name": "the_worst"}
    proposals = propose(capability(), FakeLLM([bad, GOOD]))
    assert [p.outcome.name for p in proposals] == ["member_not_found"]


def test_a_proposal_with_an_unknown_detector_kind_is_dropped() -> None:
    bad = GOOD | {"detect_kind": "vibes", "name": "feels_wrong"}
    proposals = propose(capability(), FakeLLM([bad, GOOD]))
    assert [p.outcome.name for p in proposals] == ["member_not_found"]


def test_a_proposal_naming_a_step_that_does_not_exist_falls_back_to_any() -> None:
    """The model hallucinating a step id should not invalidate an otherwise fine outcome."""
    invented = GOOD | {"step_id": "step-that-never-existed"}
    proposals = propose(capability(), FakeLLM([invented]))
    assert proposals[0].outcome.applies_to == "any"


def test_a_recoverable_proposal_gets_the_most_conservative_recovery() -> None:
    """The schema forbids a recoverable outcome with no recovery, and the model rarely gives one."""
    proposals = propose(capability(), FakeLLM([RECOVERABLE]))
    recovery = proposals[0].outcome.recovery
    assert recovery is not None
    assert recovery.action == "retry_step"
    assert recovery.max_attempts == 1


def test_the_number_of_proposals_is_capped() -> None:
    many = [GOOD | {"name": f"guess_{i}"} for i in range(20)]
    assert len(propose(capability(), FakeLLM(many), max_outcomes=3)) == 3


def test_a_model_that_proposes_nothing_callable_is_an_error() -> None:
    with pytest.raises(ProposalError, match="proposed nothing callable"):
        propose(capability(), FakeLLM(None, text="I think everything is fine."))


# ---- the type boundary is the safety property ----------------------------------------------


def test_a_proposal_is_not_an_outcome() -> None:
    """An unreviewed guess must not be substitutable for a reviewed fact."""
    proposal = propose(capability(), FakeLLM([GOOD]))[0]
    assert isinstance(proposal, OutcomeProposal)
    assert not isinstance(proposal, Outcome)
    assert proposal.accepted is False


def test_proposing_does_not_touch_the_capability() -> None:
    artifact = capability()
    propose(artifact, FakeLLM([GOOD, RECOVERABLE]))
    assert artifact.outcomes == []
    assert artifact.provenance.outcomes_reviewed is False


# ---- the human gate ------------------------------------------------------------------------


def test_accepted_proposals_land_in_the_capability() -> None:
    artifact = capability()
    proposals = propose(artifact, FakeLLM([GOOD, RECOVERABLE]))

    reviewed = apply_review(artifact, proposals, lambda p: p.outcome, approved_by="aditi")

    assert [o.name for o in reviewed.outcomes] == ["member_not_found", "maintenance_notice"]
    assert reviewed.provenance.outcomes_reviewed is True
    assert reviewed.provenance.approved_by == "aditi"


def test_rejected_proposals_do_not() -> None:
    artifact = capability()
    proposals = propose(artifact, FakeLLM([GOOD, RECOVERABLE]))

    reviewed = apply_review(artifact, proposals, lambda _: None, approved_by="aditi")

    assert reviewed.outcomes == []
    assert reviewed.provenance.outcomes_reviewed is True, (
        "rejecting everything is still a review; what it must not mean is nobody looked"
    )


def test_a_reviewer_can_edit_a_proposal_before_accepting_it() -> None:
    artifact = capability()
    proposals = propose(artifact, FakeLLM([GOOD]))

    def correct_the_kind(proposal: OutcomeProposal) -> Outcome:
        """The model called it business; the reviewer knows it is a hard failure."""
        return proposal.outcome.model_copy(
            update={"kind": "hard_failure", "message_template": "The search screen broke."}
        )

    reviewed = apply_review(artifact, proposals, correct_the_kind, approved_by="aditi")

    assert reviewed.outcomes[0].kind == "hard_failure"
    assert reviewed.outcomes[0].message_template == "The search screen broke."


def test_review_does_not_duplicate_an_outcome_that_already_exists() -> None:
    existing = Outcome(
        name="member_not_found",
        kind="business",
        detect=Condition(kind="text_present", params={"text": "Nothing here"}),
        message_template="Already reviewed.",
    )
    artifact = capability(outcomes=[existing])
    proposals = propose(artifact, FakeLLM([GOOD]))

    reviewed = apply_review(artifact, proposals, lambda p: p.outcome, approved_by="aditi")

    assert len(reviewed.outcomes) == 1
    assert reviewed.outcomes[0].message_template == "Already reviewed."


def test_an_accepted_outcome_is_revalidated_against_the_capability() -> None:
    """An edit that names a step the capability does not have must be caught, not saved."""
    artifact = capability()
    proposals = propose(artifact, FakeLLM([GOOD]))

    def break_it(proposal: OutcomeProposal) -> Outcome:
        return proposal.outcome.model_copy(update={"applies_to": ["ghost-step"]})

    with pytest.raises(Exception, match="unknown steps"):
        apply_review(artifact, proposals, break_it, approved_by="aditi")


# ---- approval is a separate gate from review -------------------------------------------------


def test_a_capability_cannot_be_approved_before_its_outcomes_are_reviewed() -> None:
    with pytest.raises(ProposalError, match="must be reviewed"):
        approve(capability(reviewed=False), approved_by="aditi")


def test_approval_after_review_makes_it_replayable_unattended() -> None:
    reviewed = capability(reviewed=True)
    assert not reviewed.provenance.replayable_unattended

    approved = approve(reviewed, approved_by="aditi")

    assert approved.provenance.state == "approved"
    assert approved.provenance.replayable_unattended


# ---- caching, so reviewing twice does not re-spend quota ---------------------------------------


def test_proposals_round_trip_through_disk(tmp_path: Path) -> None:
    proposals = propose(capability(), FakeLLM([GOOD, RECOVERABLE]))
    save_proposals("member.savings_balance.lookup", proposals, tmp_path)
    assert load_proposals("member.savings_balance.lookup", tmp_path) == proposals


def test_loading_proposals_that_were_never_made_returns_none(tmp_path: Path) -> None:
    assert load_proposals("never.proposed", tmp_path) is None


def test_a_proposal_renders_with_its_kind_first() -> None:
    """The kind is the decision the reviewer is actually making, so it leads."""
    proposal = propose(capability(), FakeLLM([GOOD]))[0]
    rendered = describe_proposal(proposal)
    assert rendered.startswith("BUSINESS")
    assert "member_not_found" in rendered
    assert "Search screens usually re-render" in rendered
