"""Candidate synthesis against real markup: generate, verify, score, rank.

The fixtures are the harness's own screens rather than tidy handwritten HTML, because the
whole question is whether these strategies survive nested layout tables, repeated link text
and ids that change on every render.
"""

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, sync_playwright

from cua.recording.synthesizer import SynthesisError, score, synthesize, synthesize_unverified
from cua.replay.resolver import dom_hint_count, matches, walk
from cua.schema.models import SURFACE_SPECIFIC_SCORE_CAP, Locator
from cua.surfaces.base import ActionTarget, AXNode
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
            "target_role": "LayoutTableCell",
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
            "target_role": "LayoutTableCell",
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
        if node.role == "LayoutTableCell" and "4,182.55" in ((node.name or "") + (node.value or ""))
    ]
    assert region_cells, "fixture no longer contains the balance cell"
    locator = Locator(
        strategy="region_ordinal",
        params={"region": "Recent activity", "role": "LayoutTableCell", "index": 0},
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
