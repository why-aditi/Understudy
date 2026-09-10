"""D3 pruning is what keeps an observation inside the free-tier token budget."""

from cua.surfaces.base import AXNode
from cua.surfaces.pruning import MAX_ROWS, MAX_TEXT, count_nodes, observation_hash, prune


def node(role: str, name: str | None = None, *children: AXNode, value: str | None = None) -> AXNode:
    return AXNode(role=role, name=name, value=value, children=list(children))


def test_unnamed_non_interactive_nodes_are_spliced_out_but_keep_their_children() -> None:
    tree = node("RootWebArea", "Members", node("group", None, node("link", "Open")))

    pruned, stats = prune(tree)

    assert pruned is not None
    assert [c.role for c in pruned.children] == ["link"]
    assert (stats.nodes_before, stats.nodes_after) == (3, 2)


def test_unnamed_interactive_nodes_survive() -> None:
    """An icon-only button has no accessible name and is still the thing to click."""
    tree = node("RootWebArea", "Members", node("button"))

    pruned, _ = prune(tree)

    assert pruned is not None
    assert [c.role for c in pruned.children] == ["button"]


def test_presentation_nodes_are_stripped_and_content_survives() -> None:
    tree = node(
        "RootWebArea",
        "Members",
        node("presentation", None, node("cell", "12345")),
        node("generic", None, node("cell", "Jane")),
    )

    pruned, _ = prune(tree)

    assert pruned is not None
    assert [(c.role, c.name) for c in pruned.children] == [("cell", "12345"), ("cell", "Jane")]


def test_repeated_rows_collapse_past_the_first_ten() -> None:
    rows = [node("row", f"member-{i}") for i in range(25)]
    tree = node("RootWebArea", "Members", node("table", "Results", *rows))

    pruned, stats = prune(tree)

    assert pruned is not None
    table = pruned.children[0]
    kept = [c.name for c in table.children]
    assert kept[:MAX_ROWS] == [f"member-{i}" for i in range(MAX_ROWS)]
    assert kept[MAX_ROWS:] == ["[15 more rows collapsed]"]
    assert stats.rows_collapsed == 15


def test_ten_rows_are_left_alone() -> None:
    rows = [node("row", f"member-{i}") for i in range(MAX_ROWS)]
    _, stats = prune(node("table", "Results", *rows))
    assert stats.rows_collapsed == 0


def test_long_text_is_truncated() -> None:
    tree = node("RootWebArea", "Members", node("paragraph", "x" * 500))

    pruned, stats = prune(tree)

    assert pruned is not None
    text = pruned.children[0].name
    assert text is not None
    assert len(text) == MAX_TEXT + 3 and text.endswith("...")
    assert stats.values_truncated == 1


def test_values_are_pruned_and_truncated_too() -> None:
    tree = node("RootWebArea", "Members", node("textbox", "Member id", value="y" * 500))

    pruned, stats = prune(tree)

    assert pruned is not None
    value = pruned.children[0].value
    assert value is not None and len(value) == MAX_TEXT + 3
    assert stats.values_truncated == 1


def test_ratio_reports_how_much_was_dropped() -> None:
    tree = node("RootWebArea", "Members", *[node("generic") for _ in range(9)])

    pruned, stats = prune(tree)

    assert count_nodes(pruned) == 1
    assert (stats.nodes_before, stats.nodes_after) == (10, 1)
    assert stats.ratio == 0.9


def test_an_entirely_uninteresting_tree_prunes_to_nothing() -> None:
    pruned, stats = prune(node("generic", None, node("generic")))
    assert pruned is None
    assert stats.nodes_after == 0
    assert stats.ratio == 1.0


def test_empty_tree_has_a_zero_ratio_not_a_division_error() -> None:
    _, stats = prune(None)
    assert (stats.nodes_before, stats.nodes_after, stats.ratio) == (0, 0, 0.0)


def test_observation_hash_is_stable_across_identical_trees() -> None:
    a = node("RootWebArea", "Members", node("button", "Search"))
    b = node("RootWebArea", "Members", node("button", "Search"))
    assert observation_hash("http://x/1", a) == observation_hash("http://x/1", b)


def test_observation_hash_changes_when_the_page_changes() -> None:
    before = node("RootWebArea", "Members", node("button", "Search"))
    after = node("RootWebArea", "Members", node("button", "Clear"))
    assert observation_hash("http://x/1", before) != observation_hash("http://x/1", after)


def test_observation_hash_changes_on_navigation_alone() -> None:
    """Two pages can share a shape; landing on a new url is still progress."""
    tree = node("RootWebArea", "Members", node("button", "Search"))
    assert observation_hash("http://x/1", tree) != observation_hash("http://x/2", tree)
