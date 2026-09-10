"""Walking the candidate chain, and evaluating conditions. Both are replay's decision-making.

No model is involved in any of this, which is the point: given the same artifact and the same
screen, these functions produce the same answer every time.
"""

import pytest

from cua.replay.conditions import (
    ConditionError,
    describe,
    evaluate,
    first_matching,
)
from cua.replay.engine import can_act_through
from cua.replay.resolver import (
    Attempt,
    LocatorExhausted,
    Resolver,
    matches,
)
from cua.schema.models import Condition, ControlDescriptor, Locator, Outcome
from cua.surfaces.base import AXNode


def node(role: str, name: str | None = None, *children: AXNode, value: str | None = None) -> AXNode:
    return AXNode(role=role, name=name, value=value, children=list(children))


def locator(strategy: str, score: float, **params: object) -> Locator:
    return Locator(
        strategy=strategy,  # type: ignore[arg-type]
        params=params,
        stability_score=score,
        verified_unique_at_record=True,
    )


@pytest.fixture
def screen() -> AXNode:
    """A member row and a balance row, the two shapes replay actually meets."""
    return node(
        "RootWebArea",
        "Member 12345",
        node("heading", "Sub-accounts"),
        node(
            "table",
            None,
            node(
                "row",
                None,
                node("cell", "SAV-88120"),
                node("cell", "Savings"),
                node("cell", "Open", node("link", "Open")),
            ),
            node(
                "row",
                None,
                node("cell", "CHQ-40771"),
                node("cell", "Chequing"),
                node("cell", "Open", node("link", "Open")),
            ),
        ),
        node("textbox", "Member ID", value="12345"),
    )


def chain(*candidates: Locator) -> ControlDescriptor:
    return ControlDescriptor(role="link", name="Open", candidates=list(candidates))


# Scores are the ones recorded at synthesis time, and rank order follows them - a candidate
# that has since broken keeps the high score it earned when it was verified.
WORKING_ANCHOR = locator(
    "anchor_relative",
    0.95,
    anchor_text="SAV-88120",
    relation="same_row",
    target_role="link",
    index=0,
)
BROKEN_ANCHOR = locator(
    "anchor_relative", 0.90, anchor_text="ISA-00000", relation="same_row", target_role="link"
)
AMBIGUOUS_ROLE_NAME = locator("role_name", 0.85, role="link", name="Open", match="exact")
# A weak but still working candidate, scored below the 0.3 cap so a dom_hint ranks above it.
WEAK_TEXT = locator("text_content", 0.20, text="SAV-88120", match="exact")
DOM_HINT = locator("dom_hint", 0.9, css="#e6a7b8c9")  # capped to 0.3 by the schema


# ---- the chain ----------------------------------------------------------------------------


def test_the_primary_candidate_fires_and_no_drift_is_reported(screen: AXNode) -> None:
    resolution = Resolver().resolve(screen, chain(WORKING_ANCHOR, AMBIGUOUS_ROLE_NAME))

    assert resolution.strategy == "anchor_relative"
    assert resolution.node is not None and resolution.node.role == "link"
    assert resolution.drift is None
    assert [a.describe() for a in resolution.attempts] == ["anchor_relative matched 1"]


def test_the_chain_skips_a_candidate_that_finds_nothing(screen: AXNode) -> None:
    resolution = Resolver().resolve(screen, chain(BROKEN_ANCHOR, WEAK_TEXT))

    assert resolution.strategy == "text_content"
    assert [a.matched for a in resolution.attempts] == [0, 1]


def test_the_chain_skips_a_candidate_that_became_ambiguous(screen: AXNode) -> None:
    """Two matches is a failure, not a coin flip: it would pick the wrong control silently."""
    resolution = Resolver().resolve(screen, chain(AMBIGUOUS_ROLE_NAME, WEAK_TEXT))

    assert resolution.strategy == "text_content"
    assert resolution.attempts[0].matched == 2


def test_a_non_primary_candidate_firing_emits_a_drift_signal(screen: AXNode) -> None:
    resolution = Resolver().resolve(screen, chain(BROKEN_ANCHOR, WEAK_TEXT))

    assert resolution.drift is not None
    assert "rank 1" in resolution.drift
    assert "anchor_relative matched 0" in resolution.drift


def test_rank_order_is_the_artifact_s_order_not_the_call_order(screen: AXNode) -> None:
    """ControlDescriptor sorts by score, so a lower-scored candidate cannot jump the queue."""
    descriptor = chain(WEAK_TEXT, WORKING_ANCHOR)  # passed worst-first on purpose
    assert descriptor.primary is WORKING_ANCHOR
    assert Resolver().resolve(screen, descriptor).drift is None


def test_an_exhausted_chain_reports_every_candidate_and_what_it_matched(screen: AXNode) -> None:
    descriptor = ControlDescriptor(
        role="link",
        name="Open",
        candidates=[
            BROKEN_ANCHOR,
            AMBIGUOUS_ROLE_NAME,
            locator("text_content", 0.5, text="Nonexistent", match="exact"),
        ],
    )

    with pytest.raises(LocatorExhausted) as raised:
        Resolver().resolve(screen, descriptor)

    message = str(raised.value)
    assert "anchor_relative matched 0" in message
    assert "role_name matched 2" in message
    assert "text_content matched 0" in message
    # Attempted in rank order: 0.90, 0.85, 0.50.
    assert [a.strategy for a in raised.value.attempts] == [
        "anchor_relative",
        "role_name",
        "text_content",
    ]
    assert raised.value.descriptor.name == "Open"


def test_the_failure_distinguishes_found_nothing_from_found_too_much(screen: AXNode) -> None:
    with pytest.raises(LocatorExhausted) as raised:
        Resolver().resolve(screen, chain(BROKEN_ANCHOR, AMBIGUOUS_ROLE_NAME))

    matched = {a.strategy: a.matched for a in raised.value.attempts}
    assert matched["anchor_relative"] == 0
    assert matched["role_name"] == 2


# ---- surface agnosticism -------------------------------------------------------------------


def test_the_web_resolver_skips_no_portable_strategy(screen: AXNode) -> None:
    """Every strategy that is not surface-specific must be attempted on a web surface."""
    portable = [
        locator("anchor_relative", 0.9, anchor_text="nope", relation="same_row", target_role="x"),
        locator("region_ordinal", 0.7, region="nope", role="x", index=0),
        locator("role_name", 0.6, role="x", name="nope"),
        locator("text_content", 0.5, text="nope"),
    ]
    descriptor = ControlDescriptor(role="link", name="Open", candidates=portable)

    with pytest.raises(LocatorExhausted) as raised:
        Resolver().resolve(screen, descriptor)

    attempted = {a.strategy for a in raised.value.attempts if not a.skipped}
    assert attempted == {"anchor_relative", "region_ordinal", "role_name", "text_content"}
    assert not any(a.skipped for a in raised.value.attempts)


def test_a_non_web_resolver_skips_the_dom_hint_rather_than_failing_it(screen: AXNode) -> None:
    """Skipped is not the same as failed: the surface cannot express it, so it does not count."""
    descriptor = ControlDescriptor(
        role="link", name="Open", candidates=[BROKEN_ANCHOR, DOM_HINT, WEAK_TEXT]
    )
    desktop = Resolver(supports_surface_specific=False)

    resolution = desktop.resolve(screen, descriptor)

    skipped = [a for a in resolution.attempts if a.skipped]
    assert [a.strategy for a in skipped] == ["dom_hint"]
    assert skipped[0].matched is None
    assert "non-web surface" in (skipped[0].note or "")
    # Skipping it did not end the walk: the chain carried on and something else fired.
    assert resolution.strategy == "text_content"


def test_a_dom_hint_is_skipped_when_there_is_no_dom_to_resolve_against(screen: AXNode) -> None:
    descriptor = ControlDescriptor(
        role="link", name="Open", candidates=[BROKEN_ANCHOR, DOM_HINT, WEAK_TEXT]
    )

    resolution = Resolver(page=None).resolve(screen, descriptor)

    hint = next(a for a in resolution.attempts if a.strategy == "dom_hint")
    assert hint.skipped
    assert "no DOM available" in (hint.note or "")


def test_a_non_web_resolver_still_fails_when_only_a_dom_hint_remains(screen: AXNode) -> None:
    """A capability that leans on a css selector simply does not port, and says so."""
    descriptor = ControlDescriptor(role="link", name="Open", candidates=[DOM_HINT])

    with pytest.raises(LocatorExhausted) as raised:
        Resolver(supports_surface_specific=False).resolve(screen, descriptor)

    assert raised.value.attempts[0].skipped
    assert "skipped" in str(raised.value)


def test_matches_never_resolves_a_dom_hint_against_the_tree(screen: AXNode) -> None:
    assert matches(screen, DOM_HINT) == []


def test_an_attempt_describes_itself_for_a_failure_report() -> None:
    assert Attempt("role_name", 3).describe() == "role_name matched 3"
    assert Attempt("dom_hint", None, True, "no DOM").describe() == "dom_hint (skipped: no DOM)"


# ---- conditions: one evaluator, five kinds ---------------------------------------------------


def test_control_present_and_absent_are_the_same_question(screen: AXNode) -> None:
    present = Condition(kind="control_present", params={"role": "textbox", "name": "Member ID"})
    absent = Condition(kind="control_absent", params={"role": "textbox", "name": "Member ID"})
    assert evaluate(present, screen) is True
    assert evaluate(absent, screen) is False


def test_control_absent_is_true_for_a_control_that_is_not_there(screen: AXNode) -> None:
    condition = Condition(kind="control_absent", params={"role": "button", "name": "Delete"})
    assert evaluate(condition, screen) is True


def test_text_present_searches_names_and_values(screen: AXNode) -> None:
    assert evaluate(Condition(kind="text_present", params={"text": "CHQ-40771"}), screen)
    assert evaluate(Condition(kind="text_present", params={"text": "12345"}), screen)
    assert not evaluate(Condition(kind="text_present", params={"text": "ISA-99999"}), screen)


def test_url_matches_is_a_regex(screen: AXNode) -> None:
    condition = Condition(kind="url_matches", params={"pattern": r".*/members/\d+$"})
    assert evaluate(condition, screen, "http://x/tenant-a/members/12345")
    assert not evaluate(condition, screen, "http://x/tenant-a/")


def test_value_equals_reads_the_control_s_value(screen: AXNode) -> None:
    condition = Condition(
        kind="value_equals", params={"role": "textbox", "name": "Member ID", "value": "12345"}
    )
    assert evaluate(condition, screen)
    assert not evaluate(
        Condition(
            kind="value_equals",
            params={"role": "textbox", "name": "Member ID", "value": "99999"},
        ),
        screen,
    )


def test_negate_inverts_any_kind(screen: AXNode) -> None:
    condition = Condition(kind="text_present", params={"text": "CHQ-40771"}, negate=True)
    assert evaluate(condition, screen) is False


def test_an_unevaluable_condition_raises_rather_than_returning_false(screen: AXNode) -> None:
    """Silently returning False would make a broken checkpoint look like a failed step."""
    with pytest.raises(ConditionError, match="needs a 'text' parameter"):
        evaluate(Condition(kind="text_present", params={}), screen)
    with pytest.raises(ConditionError, match="not a valid regex"):
        evaluate(Condition(kind="url_matches", params={"pattern": "["}), screen, "http://x/")


def test_a_condition_on_a_missing_tree_is_false_not_an_error() -> None:
    """A frameset gives no tree; that is an unmet checkpoint, not a broken artifact."""
    assert evaluate(Condition(kind="control_present", params={"role": "link"}), None) is False
    assert evaluate(Condition(kind="text_present", params={"text": "x"}), None) is False


# ---- the same evaluator serves both roles ------------------------------------------------------


def test_one_evaluator_serves_checkpoints_and_detectors(screen: AXNode) -> None:
    """The identical Condition, asked as a checkpoint and as a detector, must agree."""
    condition = Condition(kind="text_present", params={"text": "CHQ-40771"})

    as_checkpoint = evaluate(condition, screen)
    outcome = Outcome(
        name="chequing_visible",
        kind="business",
        detect=condition,
        message_template="Chequing is on screen.",
    )
    as_detector = first_matching([outcome], screen, "", "any-step")

    assert as_checkpoint is True
    assert as_detector is outcome


def test_detectors_scoped_to_another_step_do_not_fire(screen: AXNode) -> None:
    outcome = Outcome(
        name="member_not_found",
        kind="business",
        detect=Condition(kind="text_present", params={"text": "Savings"}),
        applies_to=["step-one"],
        message_template="No member.",
    )
    assert first_matching([outcome], screen, "", "step-two") is None
    assert first_matching([outcome], screen, "", "step-one") is outcome


def test_the_first_declared_detector_wins(screen: AXNode) -> None:
    """Artifact order is the priority order, so a specific detector can precede a general one."""
    specific = Outcome(
        name="specific",
        kind="business",
        detect=Condition(kind="text_present", params={"text": "SAV-88120"}),
        message_template="specific",
    )
    general = Outcome(
        name="general",
        kind="business",
        detect=Condition(kind="text_present", params={"text": "Sub-accounts"}),
        message_template="general",
    )
    assert first_matching([specific, general], screen, "", "s") is specific
    assert first_matching([general, specific], screen, "", "s") is general


def test_describe_renders_a_condition_for_a_failure_report() -> None:
    condition = Condition(kind="control_present", params={"role": "link", "name": "Open"})
    assert describe(condition) == "control_present(name='Open', role='link')"
    assert describe(condition.model_copy(update={"negate": True})).startswith("not ")


def test_a_candidate_the_surface_cannot_express_is_not_counted_as_drift() -> None:
    """Drift means the recorded primary stopped working. A skip is not that.

    A candidate the surface can never act through was never going to resolve, on any run,
    against any screen - that is a fact about the surface, not evidence the UI moved.
    Counting it raised a drift signal on every single replay, which would eventually demote
    a capability that was working perfectly.
    """
    descriptor = ControlDescriptor(
        role="cell",
        name="Balance",
        candidates=[
            Locator(
                strategy="anchor_relative",
                params={"anchor_text": "Member", "relation": "following", "target_role": "cell"},
                stability_score=0.9,
                verified_unique_at_record=True,
            ),
            Locator(
                strategy="role_name",
                params={"role": "cell", "name": "Balance", "match": "exact"},
                stability_score=0.8,
                verified_unique_at_record=True,
            ),
        ],
    )
    tree = node("RootWebArea", None, node("cell", "Member"), node("cell", "Balance"))

    resolution = Resolver(actionable=can_act_through).resolve(tree, descriptor)

    assert resolution.strategy == "role_name"
    assert [a.skipped for a in resolution.attempts] == [True, False]
    assert resolution.drift is None, "a skip is not a fallback"


def test_a_candidate_that_genuinely_failed_to_resolve_is_still_drift() -> None:
    """The exemption is for skips only; a primary that matched nothing is real drift."""
    descriptor = ControlDescriptor(
        role="cell",
        name="Balance",
        candidates=[
            Locator(
                strategy="role_name",
                params={"role": "cell", "name": "Gone", "match": "exact"},
                stability_score=0.9,
                verified_unique_at_record=True,
            ),
            Locator(
                strategy="role_name",
                params={"role": "cell", "name": "Balance", "match": "exact"},
                stability_score=0.8,
                verified_unique_at_record=True,
            ),
        ],
    )
    tree = node("RootWebArea", None, node("cell", "Balance"))

    resolution = Resolver(actionable=can_act_through).resolve(tree, descriptor)

    assert resolution.drift is not None
    assert "fell back to" in resolution.drift
