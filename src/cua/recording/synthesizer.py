"""LocatorSynthesizer: ranked locator candidates, each verified unique at record time.

Given a control that a discovery run actually acted on, this proposes every way of finding
it again, then throws away the ones that do not survive contact with the live page. A
candidate that resolves to two nodes is worse than no candidate at all, because it will pick
the wrong one silently.

`verified_unique_at_record` is a fact here, not an intention: when verification runs, every
surviving candidate resolved to exactly one node.
"""

import logging
from dataclasses import dataclass

from playwright.sync_api import Page

from cua.replay.resolver import (
    HEADING_ROLES,
    ROW_ROLES,
    ancestors,
    depends_on_generated_id,
    dom_hint_count,
    matches,
    parents,
    text_of,
    walk,
)
from cua.schema.models import SURFACE_SPECIFIC_SCORE_CAP, ControlDescriptor, Locator
from cua.surfaces.base import ActionTarget, AXNode

_log = logging.getLogger(__name__)

# PRD 5.6. Heuristic, not measured - which the report says out loud.
BASE_SCORE = 0.4
HAS_ACCESSIBLE_NAME = 0.30
ANCHORED_TO_LABEL = 0.25
SPECIFIC_ROLE = 0.15
INDEX_DEPENDENT = -0.20
GENERATED_ID_DEPENDENT = -0.40

# Roles that identify one kind of control, as opposed to a container or a blob of text.
SPECIFIC_ROLES = frozenset(
    {
        "button",
        "textbox",
        "searchbox",
        "link",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "menuitem",
        "tab",
        "switch",
        "slider",
        "spinbutton",
        "cell",
        "columnheader",
        "rowheader",
    }
)


class SynthesisError(Exception):
    """The acted-on control could not be found in the observation it was acted on."""


@dataclass(frozen=True)
class Anchor:
    """A piece of nearby text that identifies which control we mean."""

    text: str
    role: str


def score(locator: Locator) -> float:
    """The PRD 5.6 heuristic, clamped to 0-1 and capped for surface-specific candidates."""
    value = BASE_SCORE
    if locator.params.get("name") or locator.params.get("text"):
        value += HAS_ACCESSIBLE_NAME
    if locator.strategy == "anchor_relative" and locator.params.get("anchor_text"):
        value += ANCHORED_TO_LABEL
    role = str(locator.params.get("target_role") or locator.params.get("role") or "")
    if role in SPECIFIC_ROLES:
        value += SPECIFIC_ROLE
    if int(locator.params.get("index", 0) or 0) > 0:
        value += INDEX_DEPENDENT
    if depends_on_generated_id(locator):
        value += GENERATED_ID_DEPENDENT

    value = max(0.0, min(1.0, value))
    if locator.strategy == "dom_hint":
        value = min(value, SURFACE_SPECIFIC_SCORE_CAP)
    return round(value, 3)


def _locator(strategy: str, **params: object) -> Locator:
    draft = Locator(
        strategy=strategy,  # type: ignore[arg-type]
        params=params,
        stability_score=0.0,
        verified_unique_at_record=False,
    )
    return draft.model_copy(update={"stability_score": score(draft)})


def _subtree_text(node: AXNode) -> str:
    return " ".join(text_of(n) for n in walk(node) if text_of(n))


def _tightest_container_size(node: AXNode, near: str, table: dict[int, AXNode]) -> int | None:
    """Size of the smallest ancestor whose contents mention `near`, or None if none do.

    A row has no accessible name of its own - the anchor text sits in a sibling cell - so
    containment is about what an ancestor *contains*, not what it is called. Size breaks the
    tie, because the document root contains every anchor on the page.
    """
    # ponytail: recomputed per candidate. Trees are one screen; memoise if that changes.
    sizes = [
        sum(1 for _ in walk(ancestor))
        for ancestor in ancestors(node, table)
        if near.lower() in _subtree_text(ancestor).lower()
    ]
    return min(sizes) if sizes else None


def find_target(tree: AXNode, target: ActionTarget) -> AXNode:
    """The node a discovery step actually acted on, located the way the step located it."""
    found = [
        node
        for node in walk(tree)
        if node.role == target.role
        and (target.name is None or (node.name or "").strip() == target.name.strip())
    ]
    if target.near and found:
        table = parents(tree)
        scoped = [(node, _tightest_container_size(node, target.near, table)) for node in found]
        reachable = [(node, size) for node, size in scoped if size is not None]
        if reachable:
            smallest = min(size for _, size in reachable)
            found = [node for node, size in reachable if size == smallest]
    if not found:
        raise SynthesisError(
            f"no {target.role!r} named {target.name!r} in the observation it was acted on"
        )
    if target.nth >= len(found):
        raise SynthesisError(f"target nth={target.nth} but only {len(found)} nodes matched")
    return found[target.nth]


def _row_anchors(node: AXNode, tree: AXNode, table: dict[int, AXNode]) -> list[Anchor]:
    """Text in the same table row that is not the control's own label."""
    row = next((a for a in ancestors(node, table) if a.role in ROW_ROLES), None)
    if row is None:
        return []
    own = (node.name or "").strip().lower()
    return [
        Anchor(text=text_of(sibling), role=sibling.role)
        for sibling in walk(row)
        if sibling is not node and text_of(sibling) and text_of(sibling).strip().lower() != own
    ]


def _preceding_heading(node: AXNode, tree: AXNode) -> str | None:
    """The heading that introduces the section this control sits in."""
    ordered = list(walk(tree))
    position = next((i for i, n in enumerate(ordered) if n is node), None)
    if position is None:
        return None
    for earlier in reversed(ordered[:position]):
        if earlier.role in HEADING_ROLES and text_of(earlier):
            return text_of(earlier)
    return None


def _preceding_named(node: AXNode, tree: AXNode) -> Anchor | None:
    """The nearest named thing before this control, for a `following` relation."""
    ordered = list(walk(tree))
    position = next((i for i, n in enumerate(ordered) if n is node), None)
    if position is None:
        return None
    for earlier in reversed(ordered[:position]):
        if earlier is not node and text_of(earlier) and earlier.role not in ROW_ROLES:
            return Anchor(text=text_of(earlier), role=earlier.role)
    return None


def propose(tree: AXNode, node: AXNode) -> list[Locator]:
    """Every candidate worth trying for this control, before verification prunes them."""
    table = parents(tree)
    drafts: list[Locator] = []

    if node.name:
        drafts.append(_locator("role_name", role=node.role, name=node.name, match="exact"))

    # anchor_relative is the primary strategy on a legacy surface, so it gets all three
    # relations rather than whichever one happens to fit first.
    for anchor in _row_anchors(node, tree, table)[:4]:
        siblings = [
            n
            for n in walk(next(a for a in ancestors(node, table) if a.role in ROW_ROLES))
            if n.role == node.role
        ]
        index = next((i for i, n in enumerate(siblings) if n is node), 0)
        drafts.append(
            _locator(
                "anchor_relative",
                anchor_text=anchor.text,
                anchor_role=anchor.role,
                relation="same_row",
                target_role=node.role,
                index=index,
            )
        )

    preceding = _preceding_named(node, tree)
    if preceding:
        drafts.append(
            _locator(
                "anchor_relative",
                anchor_text=preceding.text,
                anchor_role=preceding.role,
                relation="following",
                target_role=node.role,
                index=0,
            )
        )

    heading = _preceding_heading(node, tree)
    if heading:
        in_region = [
            n for n in walk(tree) if n.role == node.role and _preceding_heading(n, tree) == heading
        ]
        index = next((i for i, n in enumerate(in_region) if n is node), 0)
        drafts.append(
            _locator(
                "anchor_relative",
                anchor_text=heading,
                relation="within_region",
                target_role=node.role,
                index=index,
            )
        )
        drafts.append(_locator("region_ordinal", region=heading, role=node.role, index=index))

    if node.name:
        drafts.append(_locator("text_content", text=node.name, match="exact"))

    return drafts


def verify(tree: AXNode, node: AXNode, drafts: list[Locator], page: Page | None) -> list[Locator]:
    """Keep only the candidates that resolve to exactly this one node.

    Two nodes is a silent wrong-click waiting to happen, and zero is a candidate that never
    worked. Both are discarded rather than recorded with a caveat.
    """
    kept: list[Locator] = []
    for draft in drafts:
        if draft.strategy == "dom_hint":
            if page is None:
                continue
            unique = dom_hint_count(page, draft) == 1
        else:
            found = matches(tree, draft)
            unique = len(found) == 1 and found[0] is node
        if unique:
            kept.append(draft.model_copy(update={"verified_unique_at_record": True}))
    return kept


def synthesize(
    tree: AXNode,
    target: ActionTarget,
    *,
    page: Page | None = None,
    dom_hint: str | None = None,
) -> ControlDescriptor:
    """Ranked, verified locator candidates for one acted-on control.

    Pass `page` and `dom_hint` to include the terminal css fallback; it is verified against
    the DOM because it cannot be verified any other way, and it is capped at 0.3 so it can
    never outrank a portable candidate.
    """
    node = find_target(tree, target)
    drafts = propose(tree, node)
    if dom_hint:
        drafts.append(_locator("dom_hint", css=dom_hint))

    kept = verify(tree, node, drafts, page)
    _log.info(
        "locators_synthesized",
        extra={
            # Not "name": logging reserves it on LogRecord and raises if extra shadows it.
            "control_role": node.role,
            "control_name": node.name,
            "proposed": len(drafts),
            "verified": len(kept),
            "strategies": [c.strategy for c in kept],
        },
    )
    if not kept:
        raise SynthesisError(
            f"no candidate for {node.role!r} named {node.name!r} resolved uniquely; "
            f"{len(drafts)} were proposed"
        )
    # ControlDescriptor sorts on construction, so index 0 is the primary by definition.
    return ControlDescriptor(role=node.role, name=node.name, candidates=kept)


def synthesize_unverified(tree: AXNode, target: ActionTarget) -> ControlDescriptor:
    """Candidates without a live page to check them against.

    Every candidate carries verified_unique_at_record=False, which is what makes that field
    worth reading: an artifact built this way has not earned the same trust.
    """
    node = find_target(tree, target)
    drafts = propose(tree, node)
    if not drafts:
        raise SynthesisError(f"nothing to propose for {node.role!r} named {node.name!r}")
    return ControlDescriptor(role=node.role, name=node.name, candidates=drafts)
