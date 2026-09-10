"""Resolves a control through its ranked candidate chain and records which strategy fired.

Every strategy but one resolves against the accessibility tree rather than the DOM. That is
the point of the artifact: `same_row`, `following` and `within_region` are relationships any
accessibility tree models, so the same locator params resolve against a Windows UIA tree with
the same code. `dom_hint` is the exception, and is confined to the one function at the bottom.
"""

import re
from collections.abc import Iterator

from playwright.sync_api import Page

from cua.schema.models import Locator
from cua.surfaces.base import AXNode

# Roles that behave like a table row, including the layout tables legacy apps are built from.
ROW_ROLES = frozenset({"row", "LayoutTableRow"})
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
