"""Candidate synthesis against real markup: generate, verify, score, rank.

The fixtures are the harness's own screens rather than tidy handwritten HTML, because the
whole question is whether these strategies survive nested layout tables, repeated link text
and ids that change on every render.
"""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, sync_playwright

from cua.recording.synthesizer import (
    SynthesisError,
    apply_bindings,
    find_target,
    score,
    synthesize,
    synthesize_unverified,
)
from cua.replay.resolver import dom_hint_count, matches, walk
from cua.schema.models import SURFACE_SPECIFIC_SCORE_CAP, Locator
from cua.session.lock import ControlLock
from cua.surfaces.base import Action, ActionTarget, AXNode
from cua.surfaces.web import WebSurface

# A member's sub-account table, as the harness renders it: layout chrome wrapping a data
# table whose rows all end in an identically named "Open" link, ids regenerated per render.
ACCOUNTS_HTML = """
<table id="e11aa22b"><tr><td id="e33cc44d">
  <h1 id="e55ee66f">Sub-accounts</h1>
  <table border="1" id="e77aa88b">
    <tr><th id="e99cc00d">Reference</th><th id="eaabbccd">Type</th>
        <th id="edd11ee2">Opened</th><th id="eff33aa4">&nbsp;</th></tr>
    <tr><td id="e12ab34c">SAV-88120</td><td id="e56de78f">Savings</td>
        <td id="e9a0bc1d">2019-03-14</td>
        <td id="e2e3f405"><a href="/a" id="e6a7b8c9">Open</a></td></tr>
    <tr><td id="ed0e1f23">CHQ-40771</td><td id="e4a5b6c7">Chequing</td>
        <td id="e8d9e0f1">2019-03-14</td>
        <td id="e2a3b4c5"><a href="/b" id="e6d7e8f9">Open</a></td></tr>
    <tr><td id="e0a1b2c3">TRM-10093</td><td id="e4d5e6f7">Term deposit</td>
        <td id="e8a9b0c1">2021-08-02</td>
        <td id="e2d3e4f5"><a href="/c" id="e6a7b8d9">Open</a></td></tr>
  </table>
</td></tr></table>
"""

# The sub-account card: label/value pairs in a layout table, plus a second heading.
CARD_HTML = """
<h1 id="ea1b2c3d">Account SAV-88120</h1>
<table id="eb2c3d4e">
  <tr><td id="ec3d4e5f"><b>Account reference</b></td><td id="ed4e5f60">SAV-88120</td></tr>
  <tr><td id="ee5f6071"><b>Account type</b></td><td id="ef607182">Savings</td></tr>
  <tr><td id="e6071829"><b>Savings Balance</b></td><td id="e7182930">4,182.55</td></tr>
</table>
<h1 id="e8293041">Recent activity</h1>
<table id="e9304152"><tr><td id="ea415263">No activity</td></tr></table>
"""

# A frameset: the content lives in child documents the main-frame AX tree cannot see.
FRAMESET_HTML = """
<frameset rows="130,*">
  <frame name="summary" src="data:text/html,<h1>Member 12345</h1>">
  <frame name="accounts" src="data:text/html,<a href='/x'>Open</a>">
</frameset>
"""


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        yield context.new_page()
        browser.close()


def tree_for(page: Page, html: str) -> AXNode:
    """The real Chromium accessibility tree for a fragment of markup."""
    tree = maybe_tree_for(page, html)
    assert tree is not None, "fixture produced no accessibility tree"
    return tree


def maybe_tree_for(page: Page, html: str) -> AXNode | None:
    """As above, but tolerating markup that exposes nothing at all - a frameset does."""
    page.set_content(html, wait_until="domcontentloaded")
    return WebSurface(page).observe().tree


@pytest.fixture(scope="module")
def accounts(page: Page) -> AXNode:
    return tree_for(page, ACCOUNTS_HTML)


@pytest.fixture(scope="module")
def card(page: Page) -> AXNode:
    return tree_for(page, CARD_HTML)


def strategies(descriptor: object) -> list[str]:
    return [c.strategy for c in descriptor.candidates]  # type: ignore[attr-defined]


# ---- the ambiguous case the whole design exists for --------------------------------------


def test_three_identical_links_are_told_apart_by_their_row(accounts: AXNode) -> None:
    """Every row ends in a link named "Open". Only the row content distinguishes them."""
    descriptor = synthesize(accounts, ActionTarget(role="link", name="Open", near="Savings"))

    assert descriptor.role == "link"
    assert descriptor.candidates, "no candidate survived verification"
    assert descriptor.primary.strategy == "anchor_relative"
    assert descriptor.primary.params["relation"] == "same_row"
    assert "SAV-88120" in str(descriptor.primary.params["anchor_text"]) or "Savings" in str(
        descriptor.primary.params["anchor_text"]
    )


def test_a_bare_role_name_candidate_is_discarded_when_it_is_ambiguous(accounts: AXNode) -> None:
    """role_name alone matches all three Open links, so it must not survive verification."""
    descriptor = synthesize(accounts, ActionTarget(role="link", name="Open", near="Savings"))
    assert "role_name" not in strategies(descriptor)
    assert "text_content" not in strategies(descriptor)


def test_every_surviving_candidate_resolves_to_exactly_one_node(accounts: AXNode) -> None:
    descriptor = synthesize(accounts, ActionTarget(role="link", name="Open", near="Chequing"))
    for candidate in descriptor.candidates:
        assert candidate.verified_unique_at_record is True
        assert len(matches(accounts, candidate)) == 1


def test_candidates_for_different_rows_resolve_to_different_nodes(accounts: AXNode) -> None:
    """The real test of an anchor: it must pick out its own row, not merely resolve."""
    savings = synthesize(accounts, ActionTarget(role="link", name="Open", near="Savings"))
    cheq = synthesize(accounts, ActionTarget(role="link", name="Open", near="Chequing"))
    assert matches(accounts, savings.primary)[0] is not matches(accounts, cheq.primary)[0]


# ---- the three anchor relations ----------------------------------------------------------


def test_same_row_finds_the_target_in_the_anchor_s_row(accounts: AXNode) -> None:
    locator = Locator(
        strategy="anchor_relative",
        params={
            "anchor_text": "CHQ-40771",
            "relation": "same_row",
            "target_role": "link",
            "index": 0,
        },
        stability_score=0.8,
        verified_unique_at_record=True,
    )
    found = matches(accounts, locator)
    assert len(found) == 1
    assert found[0].role == "link"


def test_following_takes_the_next_node_of_that_role(card: AXNode) -> None:
    locator = Locator(
        strategy="anchor_relative",
        params={
            "anchor_text": "Savings Balance",
            "relation": "following",
            "target_role": "cell",
            "index": 0,
        },
        stability_score=0.8,
        verified_unique_at_record=True,
    )
    found = matches(card, locator)
    assert found and "4,182.55" in (found[0].name or "") + (found[0].value or "")


def test_within_region_stays_inside_its_section(card: AXNode) -> None:
    """ "Recent activity" has its own cell; the region must not reach into the section above."""
    locator = Locator(
        strategy="anchor_relative",
        params={
            "anchor_text": "Recent activity",
            "relation": "within_region",
            "target_role": "cell",
            "index": 0,
        },
        stability_score=0.8,
        verified_unique_at_record=True,
    )
    found = matches(card, locator)
    assert len(found) == 1
    assert "No activity" in (found[0].name or "")


def test_a_region_does_not_leak_into_the_next_heading(card: AXNode) -> None:
    region_cells = [
        node
        for node in walk(card)
        if node.role == "cell" and "4,182.55" in ((node.name or "") + (node.value or ""))
    ]
    assert region_cells, "fixture no longer contains the balance cell"
    locator = Locator(
        strategy="region_ordinal",
        params={"region": "Recent activity", "role": "cell", "index": 0},
        stability_score=0.5,
        verified_unique_at_record=True,
    )
    found = matches(card, locator)
    assert found and found[0] is not region_cells[0]


# ---- scoring -------------------------------------------------------------------------------


def test_an_anchored_named_specific_candidate_outscores_a_bare_ordinal() -> None:
    anchored = Locator(
        strategy="anchor_relative",
        params={"anchor_text": "Savings", "target_role": "link", "index": 0},
        stability_score=0.0,
        verified_unique_at_record=True,
    )
    ordinal = Locator(
        strategy="region_ordinal",
        params={"region": "Sub-accounts", "role": "generic", "index": 3},
        stability_score=0.0,
        verified_unique_at_record=True,
    )
    assert score(anchored) > score(ordinal)


def test_an_index_dependent_candidate_is_penalised() -> None:
    first = Locator(
        strategy="anchor_relative",
        params={"anchor_text": "a", "target_role": "link", "index": 0},
        stability_score=0.0,
        verified_unique_at_record=True,
    )
    later = first.model_copy(update={"params": {**first.params, "index": 2}})
    assert score(later) == pytest.approx(score(first) - 0.2)


def test_a_generated_id_selector_is_penalised_and_capped() -> None:
    """Both effects at once: dom_hint caps at 0.3, and a generated id costs 0.4 first."""
    generated = Locator(
        strategy="dom_hint",
        params={"css": "#e6a7b8c9"},
        stability_score=0.0,
        verified_unique_at_record=True,
    )
    assert score(generated) <= SURFACE_SPECIFIC_SCORE_CAP
    assert score(generated) < score(
        Locator(
            strategy="dom_hint",
            params={"css": "a.open-link"},
            stability_score=0.0,
            verified_unique_at_record=True,
        )
    )


def test_candidates_come_back_ranked(accounts: AXNode) -> None:
    descriptor = synthesize(accounts, ActionTarget(role="link", name="Open", near="Term deposit"))
    scores = [c.stability_score for c in descriptor.candidates]
    assert scores == sorted(scores, reverse=True)


# ---- the generated-id case ----------------------------------------------------------------


def test_a_generated_id_hint_verifies_but_never_outranks_a_portable_candidate(
    page: Page, accounts: AXNode
) -> None:
    """The id is unique on this render and gone on the next, so it must sit last."""
    page.set_content(ACCOUNTS_HTML, wait_until="domcontentloaded")
    descriptor = synthesize(
        accounts,
        ActionTarget(role="link", name="Open", near="Savings"),
        page=page,
        dom_hint="#e6a7b8c9",
    )
    assert "dom_hint" in strategies(descriptor)
    hint = next(c for c in descriptor.candidates if c.strategy == "dom_hint")
    assert hint.surface_specific is True
    assert hint.stability_score <= SURFACE_SPECIFIC_SCORE_CAP
    assert descriptor.candidates[-1].strategy == "dom_hint"
    assert descriptor.primary.strategy != "dom_hint"
    assert [c.strategy for c in descriptor.portable_candidates] != []


def test_ids_really_do_change_between_renders() -> None:
    """The premise of the penalty: the harness regenerates every id on every render."""
    first = set(re.findall(r'id="(e[0-9a-f]{7,8})"', ACCOUNTS_HTML))
    assert first, "fixture should carry generated-looking ids"
    from apps.harness.app import eid

    assert eid() != eid()


def test_a_dom_hint_matching_several_nodes_is_discarded(page: Page, accounts: AXNode) -> None:
    page.set_content(ACCOUNTS_HTML, wait_until="domcontentloaded")
    descriptor = synthesize(
        accounts,
        ActionTarget(role="link", name="Open", near="Savings"),
        page=page,
        dom_hint="a",  # matches all three Open links
    )
    assert "dom_hint" not in strategies(descriptor)


def test_dom_hint_count_refuses_a_non_dom_hint_locator(page: Page) -> None:
    with pytest.raises(ValueError, match="called with a"):
        dom_hint_count(
            page,
            Locator(
                strategy="role_name",
                params={"role": "link"},
                stability_score=0.5,
                verified_unique_at_record=True,
            ),
        )


# ---- the frameset case ---------------------------------------------------------------------


def test_a_control_inside_a_frameset_cannot_be_synthesised(page: Page) -> None:
    """The main-frame AX tree holds nothing, so there is no candidate to record.

    This is the honest outcome for a surface we cannot yet observe: refuse to record a
    capability rather than record one whose locators were never verified.
    """
    tree = maybe_tree_for(page, FRAMESET_HTML)
    assert tree is None or len(list(walk(tree))) <= 2, (
        "a frameset should expose almost nothing on the main frame"
    )
    if tree is None:
        return  # nothing at all to record, which is the honest outcome

    with pytest.raises(SynthesisError, match="no 'link' named 'Open'"):
        synthesize(tree, ActionTarget(role="link", name="Open"))


# ---- offline generation ----------------------------------------------------------------------


def test_unverified_synthesis_marks_every_candidate_as_unverified(accounts: AXNode) -> None:
    descriptor = synthesize_unverified(accounts, ActionTarget(role="link", name="Open", nth=0))
    assert descriptor.candidates
    assert all(c.verified_unique_at_record is False for c in descriptor.candidates)


def test_synthesis_fails_loudly_when_the_target_is_not_in_the_tree(accounts: AXNode) -> None:
    with pytest.raises(SynthesisError, match="no 'button' named 'Delete'"):
        synthesize(accounts, ActionTarget(role="button", name="Delete"))


def test_synthesis_fails_when_nth_is_out_of_range(accounts: AXNode) -> None:
    with pytest.raises(SynthesisError, match="only 3 nodes matched"):
        synthesize(accounts, ActionTarget(role="link", name="Open", nth=9))


# ---- synthesis and resolution are two halves of one contract ------------------------------


def test_a_synthesised_control_resolves_back_to_the_node_it_was_recorded_from(
    accounts: AXNode,
) -> None:
    """The round trip the artifact exists for: record a control, then find it again.

    Recording and replay are the two halves that must agree. If synthesis can produce a
    descriptor the resolver cannot walk, every capability is a coin flip.
    """
    from cua.recording.synthesizer import find_target
    from cua.replay.resolver import Resolver

    for anchor in ("Savings", "Chequing", "Term deposit"):
        target = ActionTarget(role="link", name="Open", near=anchor)
        recorded = find_target(accounts, target)

        descriptor = synthesize(accounts, target)
        resolution = Resolver().resolve(accounts, descriptor)

        assert resolution.node is recorded, f"{anchor}: resolved to a different node"
        assert resolution.drift is None, f"{anchor}: primary should fire on an unchanged page"
        assert resolution.strategy == descriptor.primary.strategy


def test_a_recorded_control_survives_the_page_changing_under_it(accounts: AXNode) -> None:
    """Drop the anchor the primary depends on; a lower candidate should still find it."""
    from cua.replay.resolver import Resolver
    from cua.replay.resolver import walk as walk_tree

    target = ActionTarget(role="link", name="Open", near="Chequing")
    descriptor = synthesize(accounts, target)
    assert len(descriptor.candidates) > 1, "a chain of one cannot degrade"

    # Rewrite the reference cell, as a product rename would.
    moved = accounts.model_copy(deep=True)
    for candidate in walk_tree(moved):
        if candidate.name == "CHQ-40771":
            candidate.name = "CHQ-99999-RENAMED"
        for grandchild in candidate.children:
            if grandchild.name == "CHQ-40771":
                grandchild.name = "CHQ-99999-RENAMED"

    resolution = Resolver().resolve(moved, descriptor)
    assert resolution.node is not None
    assert resolution.drift is not None, "falling back should always be visible"


# ---- verification and acting must share one role vocabulary --------------------------------

# The account card: label/value pairs in a layout table, which Chromium reports internally as
# LayoutTableCell. Playwright's role engine has never heard of that name.
CARD_FOR_ACTING = """
<table><tr><td>
  <h1>Account SAV-88120</h1>
  <table>
    <tr><td><b>Account type</b></td><td>Savings</td></tr>
    <tr><td><b>Savings Balance</b></td><td>4,182.55</td></tr>
  </table>
</td></tr></table>
"""


def test_the_observed_roles_are_roles_a_role_locator_understands(page: Page) -> None:
    """A layout table must not leak Chromium's internal role names into the artifact.

    Recording verifies against the accessibility tree and replay acts through a role
    locator. If the two vocabularies differ, a candidate verifies as unique and is then
    unfindable - or worse, finds something else.
    """
    from cua.replay.resolver import walk as walk_tree

    tree = tree_for(page, CARD_FOR_ACTING)
    internal = sorted({n.role for n in walk_tree(tree) if n.role.startswith("Layout")})
    assert internal == [], f"internal roles leaked into the tree: {internal}"


def test_a_synthesised_candidate_can_actually_be_acted_on(page: Page) -> None:
    """The end-to-end contract: what recording verified, acting must find - and only it."""
    from cua.replay.engine import to_action_target
    from cua.replay.resolver import Resolver
    from cua.surfaces.base import Action

    tree = tree_for(page, CARD_FOR_ACTING)
    descriptor = synthesize(tree, ActionTarget(role="cell", near="Savings Balance", nth=1))

    resolution = Resolver().resolve(tree, descriptor)
    surface = WebSurface(page, ControlLock.for_automation("test"))
    result = surface.act(
        Action(kind="extract", target=to_action_target(resolution.locator, descriptor))
    )

    assert result.ok, result.error
    assert result.extracted == "4,182.55", "acted on a different node than the one recorded"


def test_every_actionable_candidate_acts_on_the_same_node(page: Page) -> None:
    """A fallback must find the same control, not merely find something.

    Landing on a *different* control is the worst outcome available: the run succeeds and
    returns the wrong answer, which no amount of downstream checking will notice.
    """
    from cua.replay.engine import can_act_through, to_action_target
    from cua.surfaces.base import Action

    tree = tree_for(page, CARD_FOR_ACTING)
    descriptor = synthesize(tree, ActionTarget(role="cell", near="Savings Balance", nth=1))
    surface = WebSurface(page, ControlLock.for_automation("test"))

    actionable = [c for c in descriptor.candidates if can_act_through(c)]
    assert actionable, "the whole chain was unactionable"

    for candidate in actionable:
        result = surface.act(Action(kind="extract", target=to_action_target(candidate, descriptor)))
        assert result.ok, f"{candidate.strategy}: {result.error}"
        assert result.extracted == "4,182.55", (
            f"{candidate.strategy} acted on a different node: {result.extracted!r}"
        )


def test_a_candidate_the_surface_cannot_act_through_is_skipped_not_misapplied(
    page: Page,
) -> None:
    """`following` resolves against a tree but has no containment equivalent to act through.

    Skipping it costs one fallback. Translating it approximately cost the wrong cell.
    """
    from cua.replay.engine import can_act_through
    from cua.replay.resolver import Resolver

    tree = tree_for(page, CARD_FOR_ACTING)
    descriptor = synthesize(tree, ActionTarget(role="cell", near="Savings Balance", nth=1))
    following = [
        c
        for c in descriptor.candidates
        if c.strategy == "anchor_relative" and c.params.get("relation") == "following"
    ]
    assert following, "the fixture no longer produces a following candidate"
    assert not can_act_through(following[0])

    # Put it first in the chain so the walk has to reach it, then confirm it is skipped
    # rather than resolved-and-misapplied.
    from cua.schema.models import ControlDescriptor

    forced = ControlDescriptor(
        role=descriptor.role,
        name=descriptor.name,
        candidates=[
            following[0].model_copy(update={"stability_score": 0.99}),
            *[c for c in descriptor.candidates if can_act_through(c)],
        ],
    )
    resolution = Resolver(actionable=can_act_through).resolve(tree, forced)

    skipped = [a for a in resolution.attempts if a.skipped]
    assert [a.strategy for a in skipped] == ["anchor_relative"]
    assert "cannot act through" in (skipped[0].note or "")
    assert can_act_through(resolution.locator), "the walk settled on an unactionable candidate"


# ---- the two resolvers must agree ------------------------------------------------------------

# The results screen, whose header cells are `columnheader` rather than `cell`. That detail is
# what pulled the two resolvers apart: the tightest *row* holding "Name" contains no cells at
# all, so a resolver that stops at the first container role finds only the outer body row and
# answers with a nav link.
RESULTS_HTML = """
<table><tr><td>
  <table><tr><td><a href="/a">Member search</a></td></tr>
         <tr><td><a href="/b">Daily reports</a></td></tr></table>
  <table>
    <tr><th>Member</th><th>Name</th><th>Status</th></tr>
    <tr><td>12345</td><td>Wilhelmina Okonkwo-Bright</td><td>Active</td>
        <td><a href="/o">Open</a></td></tr>
  </table>
</td></tr></table>
"""


@pytest.mark.parametrize(
    ("role", "near", "nth", "expected"),
    [
        # The case that exposed the divergence: no `cell` in the row holding the anchor, so
        # both resolvers have to widen to the table one level out.
        ("cell", "Name", 1, "Wilhelmina Okonkwo-Bright"),
        ("cell", "12345", 1, "Wilhelmina Okonkwo-Bright"),
        ("cell", "Active", 0, "12345"),
        ("link", "12345", 0, "Open"),
    ],
)
def test_the_tree_resolver_and_the_live_surface_agree(
    page: Page, role: str, near: str, nth: int, expected: str
) -> None:
    """`find_target` walks the accessibility tree; `WebSurface` drives the live page.

    A recorded descriptor is written by the first and executed by the second, so a target
    they disagree about produces an artifact describing a control the run never touched.
    They did disagree, silently, until both were made to take the tightest container that
    actually holds a node of the target role.
    """
    tree = tree_for(page, RESULTS_HTML)
    target = ActionTarget(role=role, near=near, nth=nth)

    lock = ControlLock("resolver-agreement")
    lock.acquire("automation", by="test")

    from_tree = find_target(tree, target)
    from_page = WebSurface(page, lock).act(Action(kind="extract", target=target))

    assert from_page.ok, from_page.error
    assert (from_page.extracted or "").strip() == expected
    assert (from_tree.name or "").strip() == expected


# ---- binding a locator to a parameter --------------------------------------------------------


def bound(**params: object) -> Locator:
    return Locator(
        strategy="anchor_relative",
        params=params,
        stability_score=0.5,
        verified_unique_at_record=True,
    )


def test_an_anchor_matching_a_parameter_value_becomes_a_bind() -> None:
    """ "The cell in the same row as 12345" only ever worked for one member.

    Bound, the same candidate means "the row containing whatever the caller passed", which is
    what makes it structural rather than a recording of one member's row.
    """
    candidate = bound(anchor_text="12345", relation="same_row", target_role="cell", index=1)

    [applied] = apply_bindings([candidate], {"12345": "member_id"}, frozenset())

    assert applied.binds == {"anchor_text": "member_id"}
    assert applied.params["anchor_text"] == "12345", "the record-time literal is kept for review"


def test_an_anchor_matching_nothing_supplied_is_left_alone() -> None:
    candidate = bound(anchor_text="Search results", relation="within_region", target_role="cell")

    [applied] = apply_bindings([candidate], {"12345": "member_id"}, frozenset())

    assert applied.binds == {}


def test_binding_lifts_a_candidate_above_one_tied_to_recorded_data() -> None:
    """The ranking has to change too, or the bound candidate never gets tried."""
    anchored = bound(anchor_text="12345", relation="same_row", target_role="cell", index=1)
    by_content = Locator(
        strategy="role_name",
        params={"role": "cell", "name": "Wilhelmina Okonkwo-Bright", "match": "exact"},
        stability_score=0.85,
        verified_unique_at_record=True,
    )

    ranked = apply_bindings(
        [anchored, by_content], {"12345": "member_id"}, frozenset({"Wilhelmina Okonkwo-Bright"})
    )

    assert ranked[0].stability_score > ranked[1].stability_score


def test_a_candidate_identified_by_the_value_being_read_is_scored_down() -> None:
    """You would have to know the answer to find the control that gives you the answer."""
    circular = Locator(
        strategy="role_name",
        params={"role": "cell", "name": "Wilhelmina Okonkwo-Bright", "match": "exact"},
        stability_score=0.0,
        verified_unique_at_record=True,
    )

    plain = score(circular)
    penalised = score(circular, frozenset({"Wilhelmina Okonkwo-Bright"}))

    assert penalised < plain


def test_a_sensitive_parameter_leaves_no_literal_in_the_artifact() -> None:
    """Binding must not become a new way for a declared-sensitive value to reach disk."""
    candidate = bound(anchor_text="QX7-4412", relation="same_row", target_role="cell")

    [applied] = apply_bindings(
        [candidate], {"QX7-4412": "code"}, frozenset(), sensitive=frozenset({"code"})
    )

    assert applied.binds == {"anchor_text": "code"}
    assert applied.params["anchor_text"] == ""
    assert "QX7-4412" not in applied.model_dump_json()
