"""The chokepoint. If these are wrong, no other safety claim in the report holds."""

from pathlib import Path

import pytest

from cua.policy.engine import PolicyContext, PolicyEngine, PolicyVerdict
from cua.policy.rules import Policy, PolicyConfigError, load_policy
from cua.surfaces.base import Action, ActionTarget

REPO = Path(__file__).resolve().parents[1]
LOCAL = "http://localhost:8080/members/12345"


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(load_policy(REPO / "policy.yaml"))


def discovery(**kwargs: object) -> PolicyContext:
    return PolicyContext.model_validate({"mode": "discovery", "current_url": LOCAL, **kwargs})


def replay(**kwargs: object) -> PolicyContext:
    return PolicyContext.model_validate({"mode": "replay", "current_url": LOCAL, **kwargs})


def click(name: str) -> Action:
    return Action(kind="click", target=ActionTarget(role="button", name=name))


# ---- the shipped policy file loads --------------------------------------------------


def test_shipped_policy_file_is_valid() -> None:
    policy = load_policy(REPO / "policy.yaml")
    assert "localhost:*" in policy.allowlist.hosts
    assert policy.risk.irreversible == ["delete", "transfer", "close", "disburse", "wire"]


def test_a_missing_policy_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match="cannot read policy"):
        load_policy(tmp_path / "nope.yaml")


def test_a_malformed_policy_file_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("allowlist: [not, a, mapping]", encoding="utf-8")
    with pytest.raises(PolicyConfigError, match="not a valid policy"):
        load_policy(bad)


# ---- safe -----------------------------------------------------------------------------


def test_reads_are_allowed(engine: PolicyEngine) -> None:
    verdict = engine.check(
        Action(kind="extract", target=ActionTarget(role="cell", name="Balance")), discovery()
    )
    assert (verdict.decision, verdict.risk) == ("allow", "safe")
    assert verdict.allowed


def test_navigate_inside_the_allowlist_is_allowed(engine: PolicyEngine) -> None:
    action = Action(kind="navigate", value="http://localhost:8080/members")
    assert engine.check(action, discovery()).decision == "allow"


def test_typing_into_a_field_is_safe(engine: PolicyEngine) -> None:
    action = Action(kind="type", target=ActionTarget(role="textbox", name="Member id"), value="1")
    assert engine.check(action, discovery()).risk == "safe"


# ---- allowlist ------------------------------------------------------------------------


def test_navigating_off_the_allowlist_is_blocked_but_not_escalated(engine: PolicyEngine) -> None:
    action = Action(kind="navigate", value="https://example.com/members")
    verdict = engine.check(action, discovery())
    assert verdict.decision == "block_and_continue"
    assert verdict.rule == "host_not_allowlisted"


def test_an_action_kind_outside_the_allowlist_is_blocked() -> None:
    policy = Policy.model_validate(
        {"allowlist": {"hosts": ["localhost:*"], "actions": ["extract"]}}
    )
    verdict = PolicyEngine(policy).check(click("Search"), discovery())
    assert verdict.rule == "action_not_allowlisted"


def test_an_unknown_url_fails_closed(engine: PolicyEngine) -> None:
    verdict = engine.check(click("Search"), discovery(current_url=None))
    assert verdict.decision == "block_and_continue"
    assert verdict.rule == "unknown_url"


def test_a_route_outside_the_allowlist_is_blocked() -> None:
    policy = Policy.model_validate(
        {"allowlist": {"hosts": ["localhost:*"], "routes": ["/members/*"], "actions": ["navigate"]}}
    )
    action = Action(kind="navigate", value="http://localhost:8080/admin/users")
    assert PolicyEngine(policy).check(action, discovery()).rule == "route_not_allowlisted"


# ---- risky ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Submit", "Save changes", "Create member"])
def test_risky_clicks_are_allowed_during_discovery(engine: PolicyEngine, name: str) -> None:
    verdict = engine.check(click(name), discovery())
    assert (verdict.decision, verdict.risk) == ("allow", "risky")


def test_risky_replay_needs_an_approved_capability(engine: PolicyEngine) -> None:
    verdict = engine.check(click("Save"), replay(capability_approved=False, step_recorded=True))
    assert verdict.decision == "block_and_continue"
    assert verdict.rule == "risky_replay_unapproved"


def test_risky_replay_needs_the_step_to_have_been_recorded(engine: PolicyEngine) -> None:
    verdict = engine.check(click("Save"), replay(capability_approved=True, step_recorded=False))
    assert verdict.rule == "risky_replay_unrecorded"


def test_an_approved_recorded_risky_step_replays(engine: PolicyEngine) -> None:
    verdict = engine.check(click("Save"), replay(capability_approved=True, step_recorded=True))
    assert verdict.decision == "allow"


# ---- irreversible ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Delete member", "Transfer funds", "Close account", "Disburse loan", "Wire transfer"]
)
def test_irreversible_targets_escalate_in_discovery(engine: PolicyEngine, name: str) -> None:
    verdict = engine.check(click(name), discovery())
    assert (verdict.decision, verdict.risk) == ("block_and_escalate", "irreversible")


def test_irreversible_targets_escalate_in_replay_too(engine: PolicyEngine) -> None:
    """Recording a delete during discovery does not license replaying it unattended."""
    verdict = engine.check(
        click("Delete member"), replay(capability_approved=True, step_recorded=True)
    )
    assert verdict.decision == "block_and_escalate"


def test_an_irreversible_route_escalates_even_with_a_harmless_control(engine: PolicyEngine) -> None:
    action = Action(kind="navigate", value="http://localhost:8080/members/12345/delete")
    assert engine.check(action, discovery()).decision == "block_and_escalate"


def test_a_step_declared_irreversible_escalates_whatever_it_is_called(engine: PolicyEngine) -> None:
    verdict = engine.check(click("Go"), replay(step_declared_irreversible=True))
    assert verdict.decision == "block_and_escalate"
    assert "declared irreversible" in verdict.reason


def test_reading_a_page_whose_route_says_delete_is_still_safe(engine: PolicyEngine) -> None:
    """Extraction changes nothing, so the route's name cannot make it dangerous."""
    action = Action(kind="extract", target=ActionTarget(role="cell", name="Status"))
    verdict = engine.check(action, discovery(current_url="http://localhost:8080/delete-log"))
    assert verdict.decision == "allow"


def test_a_word_inside_another_word_does_not_fire(engine: PolicyEngine) -> None:
    """'Closing balance' is a number to read, not an account to close."""
    assert engine.check(click("Closing balance report"), discovery()).risk != "irreversible"


def test_verdicts_are_serialisable_for_evidence(engine: PolicyEngine) -> None:
    verdict = engine.check(click("Delete member"), discovery())
    dumped = verdict.model_dump()
    assert dumped["decision"] == "block_and_escalate"
    assert PolicyVerdict.model_validate(dumped) == verdict
