"""Accessibility-tree reduction that keeps observations inside the LLM token budget."""

import hashlib
import json

from cua.surfaces.base import AXNode, PruningStats

MAX_ROWS = 10
MAX_TEXT = 200
COLLAPSE_MARKER = "[{count} more rows collapsed]"

# Nodes worth keeping even when they carry no accessible name.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
    }
)

# Nodes that exist only for layout. Their content can still matter, so they are
# spliced out and their children hoisted rather than dropped with the subtree.
PRESENTATION_ROLES = frozenset({"presentation", "none", "generic", "InlineTextBox", "LineBreak"})

# Containers that survive pruning even with no accessible name, because the anchor_relative
# strategy is expressed in terms of them: "the link in the row that says Savings" is not
# answerable once the rows have been spliced out. Keep this aligned with the container roles
# in replay/resolver.py - dropping one here silently disables a relation there.
STRUCTURAL_ROLES = frozenset(
    {
        "table",
        "LayoutTable",
        "grid",
        "treegrid",
        "row",
        "LayoutTableRow",
        "rowgroup",
        "list",
        "listitem",
        "form",
        "region",
        "article",
        "dialog",
        "main",
        "navigation",
    }
)

ROW_ROLES = frozenset({"row"})


def count_nodes(node: AXNode | None) -> int:
    """Total nodes in a tree, root included."""
    if node is None:
        return 0
    return 1 + sum(count_nodes(child) for child in node.children)


def prune(root: AXNode | None) -> tuple[AXNode | None, PruningStats]:
    """Reduce a raw AX tree per D3, returning the tree and what it cost.

    Four reductions: presentation nodes are spliced out, non-interactive unnamed nodes
    are spliced out, repeated table rows past the first 10 collapse into one marker,
    and long text values are truncated.
    """
    before = count_nodes(root)
    counters = {"rows_collapsed": 0, "values_truncated": 0}
    kept = _prune_node(root, counters) if root is not None else []

    # A tree whose own root is uninteresting can hoist into several nodes; wrap them
    # rather than silently discarding everything but the first.
    if not kept:
        pruned = None
    elif len(kept) == 1:
        pruned = kept[0]
    else:
        pruned = AXNode(role=root.role if root else "RootWebArea", children=kept)

    stats = PruningStats(
        nodes_before=before,
        nodes_after=count_nodes(pruned),
        rows_collapsed=counters["rows_collapsed"],
        values_truncated=counters["values_truncated"],
    )
    return pruned, stats


def _prune_node(node: AXNode, counters: dict[str, int]) -> list[AXNode]:
    """Prune one node, returning what should take its place in its parent."""
    children: list[AXNode] = []
    for child in node.children:
        children.extend(_prune_node(child, counters))
    children = _collapse_rows(children, counters)

    if node.role in PRESENTATION_ROLES:
        return children

    name = _truncate(node.name, counters)
    value = _truncate(node.value, counters)
    keep = node.role in INTERACTIVE_ROLES or node.role in STRUCTURAL_ROLES
    if not keep and not name and not value:
        return children

    # A structural container with nothing left in it is scaffolding, not structure.
    if node.role in STRUCTURAL_ROLES and not children and not name and not value:
        return []

    return [AXNode(role=node.role, name=name, value=value, children=children)]


def _collapse_rows(children: list[AXNode], counters: dict[str, int]) -> list[AXNode]:
    """Keep the first MAX_ROWS rows; replace the rest with a single marker node."""
    rows = [c for c in children if c.role in ROW_ROLES]
    if len(rows) <= MAX_ROWS:
        return children

    dropped = rows[MAX_ROWS:]
    counters["rows_collapsed"] += len(dropped)
    marker = AXNode(role="row", name=COLLAPSE_MARKER.format(count=len(dropped)))
    kept: list[AXNode] = []
    seen = 0
    for child in children:
        if child.role not in ROW_ROLES:
            kept.append(child)
            continue
        seen += 1
        if seen <= MAX_ROWS:
            kept.append(child)
        elif seen == MAX_ROWS + 1:
            kept.append(marker)
    return kept


def _truncate(text: str | None, counters: dict[str, int]) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) <= MAX_TEXT:
        return text
    counters["values_truncated"] += 1
    return text[:MAX_TEXT] + "..."


def observation_hash(url: str, tree: AXNode | None) -> str:
    """Stable digest of url plus pruned tree, used for no-progress detection.

    The url is part of the digest because navigating somewhere new is progress even
    when the two pages happen to share a shape.
    """
    payload = json.dumps(
        {"url": url, "tree": tree.model_dump() if tree else None},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
