"""Turn a discovery trace into a draft capability artifact.

This is the join between the two halves of the system. Discovery produces a sequence of
(tree, action) pairs; replay needs a `Capability`. Without this the artifact has to be written
by hand, which makes "record once, replay many times" a manual step dressed up as a pipeline.

What comes out is deliberately a **draft**:

- `state="draft"`, `outcomes_reviewed=False`, so it cannot replay unattended until a human
  looks at it. The model chose these controls; nobody has agreed they are the right ones.
- No outcomes. Detectors come from the proposal pass and its review gate, not from a trace of
  one successful run - a run that never hit an error screen has nothing to say about errors.
- Typed values are kept as literals. Deciding that a value should become a parameter is a
  judgement about intent, and guessing it wrong silently would produce a capability that
  looks reusable and is not.

Candidates are synthesized unverified, because verification re-resolves against a live page
and the trace is over by the time this runs. Every candidate therefore carries
`verified_unique_at_record=False`, which is exactly what that flag is for: it says nobody
proved this, rather than quietly implying somebody did.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from cua.recording.synthesizer import SynthesisError, find_target, synthesize_unverified
from cua.schema.models import (
    AppRef,
    Capability,
    ControlDescriptor,
    EntryPoint,
    OutputSpec,
    Provenance,
    Step,
)
from cua.surfaces.base import (
    Action,
    ActionTarget,
    AXNode,
)

_log = logging.getLogger(__name__)

# Surface actions map one-to-one onto step actions; the artifact has two more (`assert`,
# and `extract` used as a read) that a surface never originates.
STEP_ACTIONS = {
    "navigate": "navigate",
    "click": "click",
    "type": "type",
    "select": "select",
    "press_key": "press_key",
    "wait_for": "wait_for",
    "extract": "extract",
}


@dataclass(frozen=True)
class ActedStep:
    """One action the discovery loop took, and the tree it was looking at when it decided."""

    tree: AXNode
    action: Action
    extracted: str | None = None


def slug(text: str, limit: int = 40) -> str:
    """A dotted-identifier-safe fragment of free text."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit].rstrip("-")) or "unnamed"


def step_id(index: int, action: Action) -> str:
    """A readable, stable id: the action and what it acted on."""
    name = action.target.name if action.target and action.target.name else action.kind
    return f"{index:02d}-{slug(name, 24)}"


def _normalise(text: str) -> str:
    """Whitespace-insensitive comparison text. `inner_text` and an AX name differ in spacing."""
    return " ".join(text.split()).strip()


def _describes_what_happened(acted_step: ActedStep, target: "ActionTarget") -> bool:
    """Whether the tree resolves this target to the control the action actually hit.

    Only an `extract` carries ground truth - the surface returns the text it read - so only
    an extract can be checked. It is worth checking: the synthesizer resolves against the
    accessibility tree while the surface resolves against the live page, and when those two
    disagreed the draft recorded a descriptor for a control the run never touched, with
    nothing to say so. Found by replaying a real discovery trace against its own snapshots.
    """
    extracted = acted_step.extracted
    if acted_step.action.kind != "extract" or extracted is None:
        return True
    try:
        node = find_target(acted_step.tree, target)
    except SynthesisError:
        return False
    seen = _normalise(extracted)
    if not seen:
        return True
    subtree = _normalise(" ".join(filter(None, _texts(node))))
    return seen in subtree or subtree in seen


def _texts(node: AXNode) -> list[str]:
    out = [node.name or "", node.value or ""]
    for child in node.children:
        out.extend(_texts(child))
    return out


def _is_repeat_read(steps: list[Step], action: Action, target: ControlDescriptor | None) -> bool:
    """Whether this is the same read as the step before it."""
    if action.kind != "extract" or not steps or target is None:
        return False
    previous = steps[-1]
    return previous.action == "extract" and previous.target == target


def assemble(
    *,
    goal: str,
    run_id: str,
    model: str,
    entry_url: str,
    acted: list[ActedStep],
    tenant_id: str | None = None,
    outputs: dict[str, str] | None = None,
    vendor_product: str = "unknown",
) -> Capability:
    """One discovery trace as a draft capability. Raises nothing; produces a draft or fails."""
    steps: list[Step] = []
    extract_steps: dict[str, str] = {}

    for index, acted_step in enumerate(acted, start=1):
        action = acted_step.action
        if action.kind == "navigate":
            # The entry point is recorded on the capability, not repeated as a step.
            continue
        identifier = step_id(index, action)
        target = None
        if action.target is not None:
            if not _describes_what_happened(acted_step, action.target):
                # The descriptor would name a control this action did not touch. Recording
                # it would be worse than recording nothing: the artifact would assert a
                # control the run never used, and replay would look correct doing it.
                _log.info(
                    "assemble_descriptor_mismatch",
                    extra={"step": identifier, "extracted_len": len(acted_step.extracted or "")},
                )
                continue
            target = synthesize_unverified(acted_step.tree, action.target)

        if _is_repeat_read(steps, action, target):
            # A read has no side effect, so reading the same control twice in a row is the
            # model repeating itself, not two things the capability needs to do.
            _log.info("assemble_repeat_read_dropped", extra={"step": identifier})
            if action.kind == "extract" and acted_step.extracted is not None:
                extract_steps[acted_step.extracted] = steps[-1].id
            continue

        steps.append(
            Step(
                id=identifier,
                intent=_intent(action),
                action=STEP_ACTIONS[action.kind],  # type: ignore[arg-type]
                target=target,
                value=action.value,
            )
        )
        if action.kind == "extract" and acted_step.extracted is not None:
            extract_steps[acted_step.extracted] = identifier

    if not steps:
        raise ValueError("a capability needs at least one step; the trace acted on nothing")

    declared: list[OutputSpec] = []
    for name, value in (outputs or {}).items():
        source = extract_steps.get(value)
        if source is None:
            # Reported rather than invented: an output whose value no step extracted cannot
            # be produced on replay, and pointing it at an arbitrary step would hide that.
            _log.info(
                "assemble_output_unmatched", extra={"output": name, "value_seen": bool(value)}
            )
            continue
        declared.append(
            OutputSpec(
                name=slug(name).replace("-", "_"),
                type="string",
                source_step_id=source,
                description=f"{name}, read during discovery.",
            )
        )

    return Capability(
        id=f"draft.{slug(goal)}".replace("-", "_"),
        name=goal[:80],
        description=f"Draft recorded from a discovery run: {goal}",
        version="0.1.0",
        app=AppRef(vendor_product=vendor_product, tenant_id=tenant_id),
        entry=EntryPoint(url=entry_url),
        parameters=[],
        outputs=declared,
        steps=steps,
        outcomes=[],
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model=model,
            discovery_run_id=run_id,
            state="draft",
            outcomes_reviewed=False,
        ),
    )


def _intent(action: Action) -> str:
    target = action.target
    what = f" the {target.role} {target.name!r}" if target and target.name else ""
    return f"{action.kind}{what}".strip()
