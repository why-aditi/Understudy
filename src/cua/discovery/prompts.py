"""Prompt templates for the discovery loop."""

from cua.surfaces.base import AXNode, Observation

SYSTEM = """\
You drive an enterprise web application through its accessibility tree, one action at a \
time, to accomplish a goal.

Rules:
- Emit exactly one tool call per turn. Never two, never zero.
- Only the tools you were given exist. Do not invent tools or arguments.
- Reference a control by the role and name exactly as they appear in the tree.
- When several controls share a role and name, say which one you mean with near: the text \
of something beside it, usually the row it sits in. To open the Savings row, use \
near="Savings" instead of counting rows. Use nth only when near cannot separate them, and \
remember nth is zero-based: the first match is 0.
- To read a value printed beside a label, extract with near=<the label> and nth=1. The \
label's own cell is nth=0.
- Say your reasoning in one short sentence alongside the tool call.
- Some actions will be refused by a safety policy. A refusal is final: choose a different \
approach rather than repeating it.
- Call finish as soon as the goal is met, and put the values that answer the goal in outputs.
- Never attempt anything destructive: deleting, transferring, closing, disbursing or wiring.\
"""

_INDENT = "  "


def render_tree(node: AXNode | None, depth: int = 0) -> str:
    """The pruned AX tree as indented lines, the shape a model reads most reliably."""
    if node is None:
        return "(empty)"
    line = f"{_INDENT * depth}- {node.role}"
    if node.name:
        line += f' "{node.name}"'
    if node.value:
        line += f": {node.value}"
    lines = [line]
    lines.extend(render_tree(child, depth + 1) for child in node.children)
    return "\n".join(lines)


def render_observation(observation: Observation, step: int) -> str:
    """One turn's worth of page state."""
    return (
        f"Step {step}. url={observation.url} title={observation.title or ''}\n"
        f"Accessibility tree ({observation.pruning.nodes_after} nodes, "
        f"{observation.pruning.ratio:.0%} pruned):\n"
        f"{render_tree(observation.tree)}"
    )


def goal_message(goal: str, target: str, tenant: str) -> str:
    return f"Goal: {goal}\nStart at: {target}\nTenant: {tenant}"


def refusal_message(rule: str, reason: str) -> str:
    return f"The safety policy refused that action (rule={rule}): {reason}. Try another approach."


def result_message(ok: bool, error: str | None, extracted: str | None) -> str:
    if not ok:
        return f"That action failed: {error}"
    if extracted is not None:
        return f"That action succeeded and read: {extracted}"
    return "That action succeeded."
