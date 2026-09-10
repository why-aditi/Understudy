"""Outcome-proposal pass and the human approval gate that promotes a capability out of draft.

A model reads the recorded flow as text and guesses at what could go wrong at each step. It
is good at this in the way brainstorming is good: fast, broad, and unreliable.

**This pass will invent outcomes that cannot occur and miss outcomes that will.** It has
never seen the application fail. It is pattern-matching "member search" onto every member
search it was trained on, so it confidently proposes a `member_not_found` that the app spells
differently, a `session_timeout` this screen cannot produce, and it quietly omits the
permission error that is the one you will actually hit on a Monday morning.

That is why nothing here writes to a capability. The pass emits `OutcomeProposal`, which is
a *different type* from `Outcome` precisely so an unreviewed guess cannot be mistaken for a
reviewed fact, and only `apply_review` converts one into the other. The human gate is
load-bearing: `provenance.outcomes_reviewed` is what unattended replay checks, and it is the
difference between a capability that returns "no such member" as an answer and one that
returns it as a crash.

The pass is text-only and runs on Groq, which keeps the vision-capable Gemini quota for the
discovery loop that actually needs it (TECH-ARCH D1).
"""

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cua.llm.base import LLMClient, Message, ToolSpec
from cua.schema.models import Capability, Condition, Outcome, Recovery, Step

_log = logging.getLogger(__name__)

PROPOSALS_SUFFIX = ".proposals.json"

SYSTEM = """\
You review a recorded UI automation and propose exceptional states it should recognise.

You are guessing. You have not seen this application fail. Propose what is plausible, say so
plainly in the rationale, and do not pad the list to look thorough.

Classify every proposal into exactly one of three kinds. This distinction is the point of the
exercise, and getting it wrong is worse than omitting the outcome:

- business: a legitimate answer the caller asked for. "No member matches that id" is a
  result, not an error. The automation worked perfectly and the answer is negative.
- recoverable: a condition the replay can handle and continue past. An interstitial notice
  to dismiss, one transient slow load to retry.
- hard_failure: the automation cannot proceed and a human needs to know. Permission denied,
  an unknown screen, a session that expired mid-flow.

A detector must be checkable from the screen alone, using one of:
  control_present / control_absent (role, name), text_present (text),
  url_matches (pattern), value_equals (role, name, value).

Prefer text_present with wording the application would actually render. Do not invent a
control name that does not appear in the recorded flow.\
"""


class ProposedOutcome(BaseModel):
    """One guess, in the shape the model is asked to produce it."""

    name: str = Field(description="snake_case identifier, e.g. member_not_found.")
    kind: str = Field(description="One of: business, recoverable, hard_failure.")
    step_id: str = Field(default="", description="Step this applies to, or empty for any step.")
    detect_kind: str = Field(
        description=("control_present, control_absent, text_present, url_matches or value_equals.")
    )
    detect_params: dict[str, str] = Field(
        default_factory=dict,
        description="Arguments for the detector, e.g. {'text': 'No member matches'}.",
    )
    message_template: str = Field(description="What to tell the caller when this fires.")
    rationale: str = Field(description="Why you believe this state can occur here.")


class _Proposals(BaseModel):
    outcomes: list[ProposedOutcome] = Field(description="The proposed exceptional states.")


class OutcomeProposal(BaseModel):
    """An advisory proposal. Deliberately not an `Outcome`.

    The type boundary is the point: an `Outcome` is something a human accepted, and there is
    no code path that turns one of these into one without passing through `apply_review`.
    """

    outcome: Outcome
    step_id: str | None = None
    rationale: str = ""
    accepted: bool = False


PROPOSE_TOOL = ToolSpec(
    name="propose_outcomes",
    description="Report every exceptional state this flow should recognise.",
    parameters={
        "type": "object",
        "properties": {
            "outcomes": {
                "type": "array",
                "description": "Proposed exceptional states, most likely first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "snake_case identifier, e.g. member_not_found.",
                        },
                        "kind": {
                            "type": "string",
                            "description": "business, recoverable or hard_failure.",
                        },
                        "step_id": {
                            "type": "string",
                            "description": "Step this applies to, or empty for any step.",
                        },
                        "detect_kind": {
                            "type": "string",
                            "description": (
                                "control_present, control_absent, text_present, url_matches "
                                "or value_equals."
                            ),
                        },
                        "detect_params": {
                            "type": "object",
                            "description": 'Detector arguments, e.g. {"text": "No member"}.',
                        },
                        "message_template": {
                            "type": "string",
                            "description": "What to tell the caller when this fires.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why you believe this state can occur here.",
                        },
                    },
                    "required": [
                        "name",
                        "kind",
                        "detect_kind",
                        "message_template",
                        "rationale",
                    ],
                },
            }
        },
        "required": ["outcomes"],
    },
)


class ProposalError(Exception):
    """The proposal pass produced nothing usable."""


def render_flow(capability: Capability) -> str:
    """The recorded flow as text. No screenshots: this pass never needs pixels."""
    lines = [
        f"Capability: {capability.id}",
        f"Purpose: {capability.description}",
        f"Application: {capability.app.vendor_product} (tenant {capability.app.tenant_id})",
        f"Entry point: {capability.entry.url}",
        "",
        "Recorded steps:",
    ]
    for step in capability.steps:
        lines.append(f"  [{step.id}] {step.intent}")
        lines.append(f"      action={step.action} risk={step.risk_class}")
        if step.target is not None:
            lines.append(f"      control: {step.target.role} named {step.target.name!r}")
        if step.checkpoint is not None:
            lines.append(f"      succeeds when: {step.checkpoint.kind} {step.checkpoint.params}")
    if capability.outputs:
        lines.append("")
        lines.append("Declared outputs: " + ", ".join(o.name for o in capability.outputs))
    return "\n".join(lines)


def _to_outcome(proposed: ProposedOutcome, step_ids: set[str]) -> Outcome | None:
    """One guess into a schema-valid Outcome, or None when it cannot be made valid."""
    if proposed.kind not in {"business", "recoverable", "hard_failure"}:
        _log.info("proposal_rejected", extra={"proposal": proposed.name, "why": "unknown kind"})
        return None

    applies: list[str] | Literal["any"] = "any"
    if proposed.step_id and proposed.step_id in step_ids:
        applies = [proposed.step_id]

    try:
        condition = Condition(
            kind=proposed.detect_kind,  # type: ignore[arg-type]
            params=dict(proposed.detect_params),
        )
        return Outcome(
            name=proposed.name,
            kind=proposed.kind,  # type: ignore[arg-type]
            detect=condition,
            applies_to=applies,
            # A recoverable outcome must say how to recover; the model rarely does, so it gets
            # the most conservative remedy and the reviewer sees it spelled out.
            recovery=(
                Recovery(action="retry_step", max_attempts=1)
                if proposed.kind == "recoverable"
                else None
            ),
            message_template=proposed.message_template,
        )
    except ValidationError as exc:
        _log.info("proposal_rejected", extra={"proposal": proposed.name, "why": str(exc)[:200]})
        return None


def propose(
    capability: Capability, llm: LLMClient, *, max_outcomes: int = 8
) -> list[OutcomeProposal]:
    """Ask the model what could go wrong. Returns advisory proposals, never outcomes.

    Text only, so it runs on the cheaper text-tier provider and leaves the vision quota alone.
    """
    if llm.supports_images:
        _log.info("proposal_provider_note", extra={"note": "a text-only provider would do"})

    response = llm.complete(
        [
            Message(role="system", content=SYSTEM),
            Message(
                role="user",
                content=(
                    f"{render_flow(capability)}\n\n"
                    f"Propose at most {max_outcomes} exceptional states, most likely first."
                ),
            ),
        ],
        tools=[PROPOSE_TOOL],
    )

    calls = [c for c in response.tool_calls if c.name == PROPOSE_TOOL.name]
    if not calls:
        raise ProposalError(
            f"the model proposed nothing callable; it said: {(response.text or '')[:300]!r}"
        )

    try:
        parsed = _Proposals.model_validate(calls[0].arguments)
    except ValidationError as exc:
        raise ProposalError(f"proposals did not match the schema: {exc}") from exc

    step_ids = {step.id for step in capability.steps}
    proposals: list[OutcomeProposal] = []
    for proposed in parsed.outcomes[:max_outcomes]:
        outcome = _to_outcome(proposed, step_ids)
        if outcome is None:
            continue
        proposals.append(
            OutcomeProposal(
                outcome=outcome,
                step_id=proposed.step_id or None,
                rationale=proposed.rationale,
            )
        )

    _log.info(
        "outcomes_proposed",
        extra={
            "capability": capability.id,
            "returned": len(parsed.outcomes),
            "usable": len(proposals),
        },
    )
    return proposals


# ---- the human gate --------------------------------------------------------------------


Decide = Callable[[OutcomeProposal], Outcome | None]
"""Given a proposal, return the outcome to keep - edited if you like - or None to reject."""


def apply_review(
    capability: Capability,
    proposals: Iterable[OutcomeProposal],
    decide: Decide,
    *,
    approved_by: str,
) -> Capability:
    """Fold accepted proposals into the capability and record that a human looked.

    This is the only path from a proposal to an outcome. `outcomes_reviewed` is set even when
    everything is rejected, because "a human read these and wanted none of them" is a review;
    what it must never mean is "nobody looked".
    """
    accepted = [outcome for proposal in proposals if (outcome := decide(proposal)) is not None]

    existing = {outcome.name for outcome in capability.outcomes}
    merged = list(capability.outcomes) + [o for o in accepted if o.name not in existing]

    reviewed = capability.model_copy(
        update={
            "outcomes": merged,
            "provenance": capability.provenance.model_copy(
                update={"outcomes_reviewed": True, "approved_by": approved_by}
            ),
        }
    )
    # Re-validate: an accepted outcome may reference a step that does not exist.
    return Capability.model_validate(reviewed.model_dump(mode="json"))


def approve(capability: Capability, *, approved_by: str) -> Capability:
    """Promote a reviewed capability to approved, which is what unattended replay requires."""
    if not capability.provenance.outcomes_reviewed:
        raise ProposalError("outcomes must be reviewed before a capability can be approved")
    return capability.model_copy(
        update={
            "provenance": capability.provenance.model_copy(
                update={"state": "approved", "approved_by": approved_by}
            )
        }
    )


# ---- caching, so a review does not re-spend quota on every run ---------------------------


def proposals_path(capability_id: str, directory: Path) -> Path:
    return directory / f"{capability_id}{PROPOSALS_SUFFIX}"


def save_proposals(capability_id: str, proposals: list[OutcomeProposal], directory: Path) -> Path:
    path = proposals_path(capability_id, directory)
    payload = [proposal.model_dump(mode="json") for proposal in proposals]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_proposals(capability_id: str, directory: Path) -> list[OutcomeProposal] | None:
    """Previously proposed outcomes, or None if this capability has never been through."""
    path = proposals_path(capability_id, directory)
    if not path.exists():
        return None
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return [OutcomeProposal.model_validate(item) for item in raw]


def describe_proposal(proposal: OutcomeProposal) -> str:
    """A reviewer-facing rendering. The kind is first because it is the decision that matters."""
    outcome = proposal.outcome
    scope = ", ".join(outcome.applies_to) if outcome.applies_to != "any" else "any step"
    return (
        f"{outcome.kind.upper():<13} {outcome.name}\n"
        f"  applies to : {scope}\n"
        f"  detect     : {outcome.detect.kind} {outcome.detect.params}\n"
        f"  message    : {outcome.message_template}\n"
        f"  rationale  : {proposal.rationale}"
    )


def step_summary(step: Step) -> str:
    return f"[{step.id}] {step.intent}"
