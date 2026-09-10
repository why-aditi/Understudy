"""PolicyEngine.check(): the single chokepoint every action passes before a surface (C2)."""

import logging
from typing import Literal

from pydantic import BaseModel

from cua.policy.rules import (
    Policy,
    RiskClass,
    action_url,
    classify,
    host_allowed,
    route_allowed,
)
from cua.surfaces.base import Action

_log = logging.getLogger(__name__)

Decision = Literal["allow", "block_and_continue", "block_and_escalate"]


class PolicyContext(BaseModel):
    """What the engine needs to know beyond the action itself."""

    mode: Literal["discovery", "replay"]
    current_url: str | None = None
    capability_id: str | None = None
    capability_approved: bool = False
    step_recorded: bool = False
    step_declared_irreversible: bool = False


class PolicyVerdict(BaseModel):
    """The decision, why it was reached, and which rule reached it."""

    decision: Decision
    risk: RiskClass
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class PolicyEngine:
    """Every action from every source passes through check() before touching a surface.

    Prompt-level guardrails are not guardrails: a model that can talk itself past a rule
    was never constrained by it, so the rule lives here instead (C2).
    """

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check(self, action: Action, context: PolicyContext) -> PolicyVerdict:
        verdict = self._decide(action, context)
        _log.info(
            "policy_check",
            extra={
                "action_kind": action.kind,
                "target_role": action.target.role if action.target else None,
                "target_name": action.target.name if action.target else None,
                "mode": context.mode,
                "decision": verdict.decision,
                "risk": verdict.risk,
                "rule": verdict.rule,
                "reason": verdict.reason,
            },
        )
        return verdict

    def _decide(self, action: Action, context: PolicyContext) -> PolicyVerdict:
        risk, why = classify(
            action,
            self.policy,
            current_url=context.current_url,
            step_declared_irreversible=context.step_declared_irreversible,
        )

        # Irreversible first: an unallowlisted delete must escalate, not quietly refuse.
        if risk == "irreversible":
            return PolicyVerdict(
                decision="block_and_escalate",
                risk=risk,
                rule="irreversible",
                reason=f"{why}; irreversible actions are never model-approved",
            )

        if action.kind not in self.policy.allowlist.actions:
            return PolicyVerdict(
                decision="block_and_continue",
                risk=risk,
                rule="action_not_allowlisted",
                reason=f"action kind {action.kind!r} is not in the allowlist",
            )

        url = action_url(action, context.current_url)
        if not url:
            return PolicyVerdict(
                decision="block_and_continue",
                risk=risk,
                rule="unknown_url",
                reason="no url to check the allowlist against; failing closed",
            )
        if not host_allowed(url, self.policy.allowlist):
            return PolicyVerdict(
                decision="block_and_continue",
                risk=risk,
                rule="host_not_allowlisted",
                reason=f"host of {url!r} is not in the allowlist",
            )
        if not route_allowed(url, self.policy.allowlist):
            return PolicyVerdict(
                decision="block_and_continue",
                risk=risk,
                rule="route_not_allowlisted",
                reason=f"route of {url!r} is not in the allowlist",
            )

        if risk == "risky" and context.mode == "replay":
            # A risky click is only safe to repeat when a human approved this exact step.
            if not context.capability_approved:
                return PolicyVerdict(
                    decision="block_and_continue",
                    risk=risk,
                    rule="risky_replay_unapproved",
                    reason=f"{why}; capability is not approved for unattended replay",
                )
            if not context.step_recorded:
                return PolicyVerdict(
                    decision="block_and_continue",
                    risk=risk,
                    rule="risky_replay_unrecorded",
                    reason=f"{why}; this step was not part of the recorded capability",
                )

        return PolicyVerdict(decision="allow", risk=risk, rule=f"{risk}_allowed", reason=why)
