"""Resolves a control through its ranked candidate chain and records which strategy fired.

Every strategy but one resolves against the accessibility tree rather than the DOM. That is
the point of the artifact: `same_row`, `following` and `within_region` are relationships any
accessibility tree models, so the same locator params resolve against a Windows UIA tree with
the same code. `dom_hint` is the exception, and is confined to the one function at the bottom.
"""

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from playwright.sync_api import Page

from cua.schema.models import ControlDescriptor, Locator
from cua.surfaces.base import AXNode

_log = logging.getLogger(__name__)

# WebSurface normalises Chromium's layout-table roles to their ARIA names before we see them,
# so a legacy layout row and a real data row are both "row" here.
ROW_ROLES = frozenset({"row"})
HEADING_ROLES = frozenset({"heading", "Heading"})
REGION_ROLES = frozenset({"region", "main", "form", "navigation", "complementary", "article"})


def walk(node: AXNode) -> Iterator[AXNode]:
    """Every node in document order, root first."""
    yield node
    for child in node.children:
        yield from walk(child)


def parents(root: AXNode) -> dict[int, AXNode]:
    """Child id -> parent node. Keyed by identity: AXNodes are values and compare equal."""
    table: dict[int, AXNode] = {}
    for node in walk(root):
        for child in node.children:
            table[id(child)] = node
    return table


def ancestors(node: AXNode, table: dict[int, AXNode]) -> Iterator[AXNode]:
    current = table.get(id(node))
    while current is not None:
        yield current
        current = table.get(id(current))


def text_of(node: AXNode) -> str:
    return f"{node.name or ''} {node.value or ''}".strip()


def _matches_text(haystack: str, needle: str, match: str) -> bool:
    if match == "exact":
        return haystack.strip() == needle.strip()
    return needle.strip().lower() in haystack.strip().lower()


def _region_nodes(root: AXNode, region: str) -> list[AXNode]:
    """Nodes belonging to the section introduced by a heading, or inside a named landmark.

    A heading does not contain the section it introduces, so the section is everything in
    document order after it, up to the next heading.
    """
    ordered = list(walk(root))
    for index, node in enumerate(ordered):
        if node.role in REGION_ROLES and _matches_text(text_of(node), region, "contains"):
            return list(walk(node))
        if node.role in HEADING_ROLES and _matches_text(text_of(node), region, "contains"):
            section: list[AXNode] = []
            for later in ordered[index + 1 :]:
                if later.role in HEADING_ROLES:
                    break
                section.append(later)
            return section
    return []


def _by_role_name(root: AXNode, locator: Locator) -> list[AXNode]:
    role = str(locator.params.get("role", ""))
    name = str(locator.params.get("name", ""))
    match = str(locator.params.get("match", "exact"))
    return [
        node
        for node in walk(root)
        if node.role == role and (not name or _matches_text(node.name or "", name, match))
    ]


def _by_text(root: AXNode, locator: Locator) -> list[AXNode]:
    text = str(locator.params.get("text", ""))
    match = str(locator.params.get("match", "exact"))
    return [node for node in walk(root) if _matches_text(text_of(node), text, match)]


def _by_region_ordinal(root: AXNode, locator: Locator) -> list[AXNode]:
    region = str(locator.params.get("region", ""))
    role = str(locator.params.get("role", ""))
    index = int(locator.params.get("index", 0))
    candidates = [node for node in _region_nodes(root, region) if node.role == role]
    return [candidates[index]] if index < len(candidates) else []


def _by_anchor(root: AXNode, locator: Locator) -> list[AXNode]:
    """The three relations that make the artifact portable."""
    anchor_text = str(locator.params.get("anchor_text", ""))
    anchor_role = str(locator.params.get("anchor_role", "") or "")
    relation = str(locator.params.get("relation", "same_row"))
    target_role = str(locator.params.get("target_role", ""))
    index = int(locator.params.get("index", 0))

    ordered = list(walk(root))
    table = parents(root)
    anchors = [
        node
        for node in ordered
        if _matches_text(text_of(node), anchor_text, "contains")
        and (not anchor_role or node.role == anchor_role)
    ]

    found: list[AXNode] = []
    for anchor in anchors:
        if relation == "same_row":
            row = next((a for a in ancestors(anchor, table) if a.role in ROW_ROLES), None)
            if row is None:
                continue
            targets = [n for n in walk(row) if n.role == target_role]
        elif relation == "following":
            position = ordered.index(anchor)
            targets = [n for n in ordered[position + 1 :] if n.role == target_role]
        elif relation == "within_region":
            targets = [n for n in _region_nodes(root, anchor_text) if n.role == target_role]
        else:
            continue
        if index < len(targets):
            found.append(targets[index])

    # Several anchors can lead to the same node; uniqueness is about nodes, not paths.
    unique: list[AXNode] = []
    for node in found:
        if not any(node is seen for seen in unique):
            unique.append(node)
    return unique


def matches(root: AXNode, locator: Locator) -> list[AXNode]:
    """Every node this locator selects, resolved against an accessibility tree.

    `dom_hint` returns nothing here: it cannot be resolved without a DOM, and a caller that
    silently treated "no DOM" as "no match" would discard a valid terminal fallback. Use
    `dom_hint_count` for those.
    """
    if locator.strategy == "role_name":
        return _by_role_name(root, locator)
    if locator.strategy == "anchor_relative":
        return _by_anchor(root, locator)
    if locator.strategy == "region_ordinal":
        return _by_region_ordinal(root, locator)
    if locator.strategy == "text_content":
        return _by_text(root, locator)
    return []


def dom_hint_count(page: Page, locator: Locator) -> int:
    """How many nodes a css or xpath terminal fallback selects.

    The only place in the system that reaches into the DOM. It exists because a dom_hint
    cannot be verified any other way, and it is reachable only for a candidate already
    flagged surface_specific, which every non-web resolver skips.
    """
    if locator.strategy != "dom_hint":
        raise ValueError(f"dom_hint_count called with a {locator.strategy!r} locator")
    selector = locator.params.get("css") or locator.params.get("xpath")
    if not selector:
        return 0
    if "xpath" in locator.params:
        selector = f"xpath={selector}"
    return page.locator(str(selector)).count()


GENERATED_ID = re.compile(r"[#.](?=[a-z_-]*\d)[a-z_-]*[0-9a-f]{6,}|:nth-child\(", re.IGNORECASE)


def depends_on_generated_id(locator: Locator) -> bool:
    """Whether this candidate leans on an id that will not survive the next render."""
    selector = str(locator.params.get("css") or locator.params.get("xpath") or "")
    return bool(GENERATED_ID.search(selector))


# ---- walking the candidate chain -----------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """One candidate, and what happened when it was tried.

    `matched` is the node count: 0 means the candidate no longer finds anything, and any
    number above 1 means it became ambiguous since it was recorded. Both are failures, and
    a failure report that cannot tell them apart is not debuggable.
    """

    strategy: str
    matched: int | None
    skipped: bool = False
    note: str | None = None

    def describe(self) -> str:
        if self.skipped:
            return f"{self.strategy} (skipped: {self.note})"
        return f"{self.strategy} matched {self.matched}"


@dataclass(frozen=True)
class Resolution:
    """Which candidate actually found the control, and whether that is worrying."""

    locator: Locator
    node: AXNode | None
    attempts: list[Attempt]
    drift: str | None = None

    @property
    def strategy(self) -> str:
        return self.locator.strategy


class LocatorExhausted(Exception):
    """No candidate in the chain resolved to exactly one node.

    Carries every attempt so a failure can be read without re-running it: which strategies
    were tried, in what order, and whether each found nothing or found too much.
    """

    def __init__(self, descriptor: ControlDescriptor, attempts: list[Attempt]) -> None:
        self.descriptor = descriptor
        self.attempts = attempts
        detail = "; ".join(attempt.describe() for attempt in attempts)
        super().__init__(
            f"no candidate resolved {descriptor.role!r} named {descriptor.name!r}: {detail}"
        )


class Resolver:
    """Walks a ControlDescriptor's candidates in rank order and reports which one fired.

    Surface-agnostic by construction: the only thing a surface changes is whether it can
    attempt a `surface_specific` candidate at all. A desktop resolver sets
    `supports_surface_specific=False` and the dom_hint is skipped rather than failed, because
    "this surface cannot express that" is not the same as "that no longer finds the control".
    """

    def __init__(
        self,
        *,
        page: Page | None = None,
        supports_surface_specific: bool = True,
        actionable: Callable[[Locator], bool] | None = None,
    ) -> None:
        self.page = page
        self.supports_surface_specific = supports_surface_specific
        # Resolving a candidate and being able to *act* through it are different questions.
        # A caller that will act supplies this so an unactionable candidate is skipped rather
        # than resolved and then mis-applied to a different control.
        self.actionable = actionable

    def _count(self, tree: AXNode, locator: Locator) -> tuple[int, AXNode | None]:
        if locator.strategy == "dom_hint":
            count = dom_hint_count(self.page, locator) if self.page else 0
            return count, None
        found = matches(tree, locator)
        return len(found), found[0] if len(found) == 1 else None

    def resolve(self, tree: AXNode, descriptor: ControlDescriptor) -> Resolution:
        """The first candidate that finds exactly one node, with the trail that got there."""
        attempts: list[Attempt] = []

        for index, candidate in enumerate(descriptor.candidates):
            if candidate.surface_specific and not self.supports_surface_specific:
                attempts.append(
                    Attempt(
                        strategy=candidate.strategy,
                        matched=None,
                        skipped=True,
                        note="surface-specific candidate on a non-web surface",
                    )
                )
                continue
            if self.actionable is not None and not self.actionable(candidate):
                attempts.append(
                    Attempt(
                        strategy=candidate.strategy,
                        matched=None,
                        skipped=True,
                        note="the surface cannot act through this candidate",
                    )
                )
                continue
            if candidate.strategy == "dom_hint" and self.page is None:
                attempts.append(
                    Attempt(
                        strategy=candidate.strategy,
                        matched=None,
                        skipped=True,
                        note="no DOM available to resolve against",
                    )
                )
                continue

            count, node = self._count(tree, candidate)
            attempts.append(Attempt(strategy=candidate.strategy, matched=count))
            if count != 1:
                continue

            drift = None
            # Only candidates that were actually attempted count. A candidate skipped
            # because this surface cannot express it was never going to resolve, on any run,
            # against any screen - that is a fact about the surface, not evidence the UI
            # moved. Counting it would raise a drift signal on every single replay and
            # eventually demote a capability that is working perfectly.
            missed = [a for a in attempts[:index] if not a.skipped]
            if missed:
                # The chain did its job, and that is precisely why it is worth a signal:
                # the surface has moved far enough that the recorded primary no longer works.
                tried = ", ".join(a.describe() for a in missed)
                drift = (
                    f"{descriptor.role}/{descriptor.name!r}: fell back to "
                    f"{candidate.strategy} at rank {index} (tried {tried})"
                )
                _log.info(
                    "locator_drift",
                    extra={
                        "control_role": descriptor.role,
                        "control_name": descriptor.name,
                        "fired": candidate.strategy,
                        "rank": index,
                        "primary": descriptor.primary.strategy,
                    },
                )
            return Resolution(locator=candidate, node=node, attempts=attempts, drift=drift)

        raise LocatorExhausted(descriptor, attempts)
