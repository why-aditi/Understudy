"""ReplayEngine: deterministic executor that holds no LLM dependency of any kind (C4).

Given the same artifact and the same screen this produces the same actions every time, and
it can do so because every decision it makes was already made during discovery and written
down. The absence of a model here is the product, not an optimisation, so it is enforced
structurally: nothing this module imports, directly or transitively, reaches `cua.llm`.

The control flow is TECH-ARCH 6.2 exactly - validate params, resolve through the candidate
chain, gate on the policy chokepoint, act, detect outcomes, assert the checkpoint, extract
and type the declared outputs.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from cua.evidence.logger import RunLogger
from cua.policy.engine import PolicyContext, PolicyEngine, PolicyVerdict
from cua.replay.conditions import describe, evaluate, first_matching
from cua.replay.resolver import Attempt, LocatorExhausted, Resolution, Resolver
from cua.schema.models import (
    Capability,
    ControlDescriptor,
    FailureDetail,
    Locator,
    Outcome,
    OutcomeResult,
    Parameter,
    ParamRef,
    ReplayResult,
    Step,
)
from cua.surfaces.base import Action, ActionResult, ActionTarget, Observation, Surface

_log = logging.getLogger(__name__)

CAPABILITY_DIR = Path("capabilities")


class ReplayError(Exception):
    """The replay could not start. Distinct from a failure *during* replay."""


class ParameterError(ReplayError):
    """The supplied parameters do not satisfy the capability's declared inputs."""


# ---- parameters ----------------------------------------------------------------------------


def _coerce(value: object, declared: str, where: str) -> str | int | float | bool:
    """One value into its declared type, or a clear complaint about why it will not go."""
    if declared == "boolean" and isinstance(value, bool):
        return value
    text = value if isinstance(value, str) else str(value)
    try:
        if declared == "string":
            return text
        if declared == "integer":
            return int(text.strip())
        if declared == "number":
            return float(text.strip())
        if declared == "boolean":
            return text.strip().lower() in {"true", "1", "yes", "y"}
        if declared == "date":
            return date.fromisoformat(text.strip()).isoformat()
    except (TypeError, ValueError) as exc:
        raise ParameterError(f"{where}: {value!r} is not a valid {declared}") from exc
    raise ParameterError(f"{where}: unknown declared type {declared!r}")


def validate_params(parameters: list[Parameter], supplied: dict[str, Any]) -> dict[str, Any]:
    """Check supplied values against the declared inputs before anything is touched.

    Rejecting an unknown parameter matters as much as catching a missing one: a caller who
    misspells a name would otherwise watch the capability run happily with a default.
    """
    declared = {parameter.name: parameter for parameter in parameters}

    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise ParameterError(f"unknown parameter(s) {unknown}; expected {sorted(declared)}")

    missing = sorted(
        name for name, parameter in declared.items() if parameter.required and name not in supplied
    )
    if missing:
        raise ParameterError(f"missing required parameter(s) {missing}")

    return {
        name: _coerce(supplied[name], declared[name].type, f"parameter {name!r}")
        for name in supplied
    }


def resolve_value(step: Step, params: dict[str, Any]) -> str | None:
    """The literal a step types, with a ParamRef swapped for the caller's value."""
    if step.value is None:
        return None
    if isinstance(step.value, ParamRef):
        if step.value.param not in params:
            raise ParameterError(f"step {step.id!r} needs parameter {step.value.param!r}")
        return str(params[step.value.param])
    return step.value


# ---- turning a resolved locator into something a surface can act on -------------------------


# What the web surface can express. `ActionTarget` targets by *containment* - a control
# inside the tightest container mentioning some text - and only two strategies mean exactly
# that. `following` means "next in document order" and the region strategies mean "the nth
# node in the section under a heading"; a container is neither, and translating them
# approximately is how a replay ends up reading the label instead of the value.
#
# They stay in the artifact regardless: they verify against an accessibility tree, they are
# what a tree-walking resolver would use, and a strategy the web surface cannot act through
# today is not a strategy that is wrong.
ACTIONABLE_STRATEGIES = frozenset({"role_name", "text_content", "anchor_relative"})
ACTIONABLE_RELATIONS = frozenset({"same_row"})


def can_act_through(locator: Locator) -> bool:
    """Whether a web surface can express this candidate as something to act on."""
    if locator.strategy not in ACTIONABLE_STRATEGIES:
        return False
    if locator.strategy == "anchor_relative":
        return str(locator.params.get("relation", "same_row")) in ACTIONABLE_RELATIONS
    return True


def to_action_target(locator: Locator, descriptor: ControlDescriptor) -> ActionTarget:
    """Express the candidate that fired in the vocabulary a surface understands.

    Every portable strategy reduces to role plus name plus containment, which is exactly
    what `ActionTarget` carries - the artifact and the surface speak the same language by
    design rather than by translation.
    """
    params = locator.params
    if locator.strategy == "role_name":
        return ActionTarget(
            role=str(params.get("role", descriptor.role)),
            name=str(params.get("name") or "") or None,
            exact=str(params.get("match", "exact")) == "exact",
        )
    # The index in an anchored or ordinal candidate counts nodes of the target *role* within
    # the container. Adding the name as well would filter twice and put the index out of
    # range, so these strategies deliberately carry no name.
    if locator.strategy == "anchor_relative":
        if not can_act_through(locator):
            raise ReplayError(
                f"cannot act through a {locator.params.get('relation')!r} anchor: the surface "
                "targets by containment, not document order"
            )
        return ActionTarget(
            role=str(params.get("target_role", descriptor.role)),
            near=str(params.get("anchor_text") or "") or None,
            nth=int(params.get("index", 0) or 0),
        )
    if locator.strategy == "text_content":
        return ActionTarget(
            role=descriptor.role,
            name=str(params.get("text") or "") or None,
            exact=str(params.get("match", "exact")) == "exact",
        )
    raise ReplayError(
        f"cannot act through a {locator.strategy!r} candidate: it resolves against an "
        "accessibility tree, but the surface targets by containment and cannot express it"
    )


# ---- the run -------------------------------------------------------------------------------


@dataclass
class _RunState:
    """Everything accumulated while stepping, gathered so the result can be assembled once."""

    started: float = 0.0
    extracted: dict[str, str] = field(default_factory=dict)
    locator_usage: dict[str, str] = field(default_factory=dict)
    #: Per step, the strategies the resolver tried. Any failure can report them, not just
    #: an exhausted chain: "which candidate fired" is the first question about a bad replay.
    attempts: dict[str, list[str]] = field(default_factory=dict)
    drift: list[str] = field(default_factory=list)
    #: Per step, the recoverable outcomes recovered from. Drift detection needs this as
    #: structure, not as a log line it would have to parse back out.
    recoveries: dict[str, list[str]] = field(default_factory=dict)
    steps_executed: int = 0


class ReplayEngine:
    """Executes a capability step by step. No model, no inference, no improvisation.

    The collaborators are deliberately narrow: a surface to act on, the policy chokepoint to
    pass through, a resolver for the candidate chain and a logger for evidence. There is no
    seam here an LLM client could be injected through, which is what makes C4 checkable.
    """

    def __init__(
        self,
        *,
        surface: Surface,
        policy: PolicyEngine,
        logger: RunLogger,
        resolver: Resolver | None = None,
        allow_screenshots: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.logger = logger
        self.resolver = resolver or Resolver(actionable=can_act_through)
        # Off by default: a failure screenshot of a servicing screen is full of PII, and
        # OCR-based redaction is not something this project solves.
        self.allow_screenshots = allow_screenshots
        self._monotonic = monotonic
        self._sleep = sleep

    # -- entry point --------------------------------------------------------------------

    def run(
        self, capability: Capability, params: dict[str, Any], *, attended: bool = False
    ) -> ReplayResult:
        state = _RunState(started=self._monotonic())

        validated = validate_params(capability.parameters, params)
        for parameter in capability.parameters:
            if parameter.sensitive and parameter.name in validated:
                # Declared before the first action, so nothing can log it even on the way in.
                self.logger.declare_sensitive(parameter.name, str(validated[parameter.name]))

        self.logger.event(
            "replay_start",
            capability_id=capability.id,
            capability_version=capability.version,
            steps=len(capability.steps),
            state=capability.provenance.state,
            attended=attended,
        )

        if not attended and not capability.provenance.replayable_unattended:
            return self._failure(
                capability,
                state,
                None,
                FailureDetail(
                    step_id="<precondition>",
                    expected="an approved capability with reviewed outcomes",
                    observed=(
                        f"state={capability.provenance.state}, "
                        f"outcomes_reviewed={capability.provenance.outcomes_reviewed}"
                    ),
                ),
            )

        for step in capability.steps:
            outcome_result = self._run_step(capability, step, validated, state)
            if isinstance(outcome_result, ReplayResult):
                return outcome_result

        return self._success(capability, state)

    # -- one step -----------------------------------------------------------------------

    def _run_step(
        self, capability: Capability, step: Step, params: dict[str, Any], state: _RunState
    ) -> ReplayResult | None:
        """Execute one step. Returns a ReplayResult if the run should stop here.

        Recovery is bounded per outcome rather than per step: two different recoverable
        conditions each get their own declared budget, and neither can be spent by the other.
        """
        recovery_used: dict[str, int] = {}

        while True:
            observation = self.surface.observe()

            try:
                target = self._resolve(step, observation, state)
            except LocatorExhausted as exhausted:
                # A control that has vanished is often a screen the capability already knows
                # about: a permission panel, a session timeout, an interstitial. Reporting
                # "locator exhausted" there would throw away the one classification a human
                # reviewed, and turn a declared answer into an automation break.
                declared = first_matching(
                    capability.outcomes, observation.tree, observation.url, step.id
                )
                if declared is not None:
                    settled = self._handle_outcome(capability, state, step, declared, recovery_used)
                    if settled is not None:
                        return settled
                    continue  # recovered; try the step again
                return self._failure(
                    capability, state, step, self._locator_failure(step, exhausted.attempts)
                )
            except ReplayError as unsupported:
                return self._failure(
                    capability,
                    state,
                    step,
                    FailureDetail(
                        step_id=step.id,
                        expected="a candidate the surface can act through",
                        observed=str(unsupported),
                    ),
                )

            verdict = self.policy.check(
                Action(
                    kind=_ACTIONS[step.action], target=target, value=resolve_value(step, params)
                ),
                PolicyContext(
                    mode="replay",
                    current_url=observation.url,
                    capability_id=capability.id,
                    capability_approved=capability.provenance.state == "approved",
                    step_recorded=True,
                    step_declared_irreversible=step.risk_class == "irreversible",
                ),
            )
            if not verdict.allowed:
                return self._failure(
                    capability,
                    state,
                    step,
                    FailureDetail(
                        step_id=step.id,
                        expected=f"policy to permit a {step.risk_class} {step.action}",
                        observed=f"{verdict.decision} (rule={verdict.rule}): {verdict.reason}",
                    ),
                )

            result = self._act(step, target, params, verdict)
            state.steps_executed += 1
            if result.extracted is not None:
                state.extracted[step.id] = result.extracted

            after = self.surface.observe()
            outcome = first_matching(capability.outcomes, after.tree, after.url, step.id)

            if outcome is not None:
                # A declared situation always wins over the checkpoint: the capability
                # said this can happen and what it means, so guessing from the checkpoint
                # instead would throw away the only classification anyone reviewed.
                settled = self._handle_outcome(capability, state, step, outcome, recovery_used)
                if settled is not None:
                    return settled
                continue  # recovered; try the step again

            if step.checkpoint is not None and not evaluate(step.checkpoint, after.tree, after.url):
                # No detector matched and the checkpoint did not hold: the screen is in a
                # state nobody described. Continuing from here would be guessing, so it is a
                # hard failure rather than a silent carry-on.
                return self._failure(
                    capability,
                    state,
                    step,
                    FailureDetail(
                        step_id=step.id,
                        expected=describe(step.checkpoint),
                        observed=(
                            f"checkpoint not met at {after.url}, and no declared outcome "
                            f"explains it ({len(capability.outcomes)} detector(s) checked)"
                        ),
                    ),
                )

            if not result.ok:
                return self._failure(
                    capability,
                    state,
                    step,
                    FailureDetail(
                        step_id=step.id,
                        expected=f"{step.action} to succeed",
                        observed=result.error or "action failed with no detail",
                    ),
                )
            return None

    def _handle_outcome(
        self,
        capability: Capability,
        state: _RunState,
        step: Step,
        outcome: Outcome,
        recovery_used: dict[str, int],
    ) -> ReplayResult | None:
        """Do what this outcome's kind says to do.

        Returns a result when the run should stop, or None when the condition was recovered
        from and the step should be attempted again. One place for the three-way decision,
        because it is reached both after an action and when a control could not be found.
        """
        if outcome.kind == "business":
            return self._business(capability, state, outcome)

        if outcome.kind == "hard_failure":
            return self._failure(
                capability,
                state,
                step,
                FailureDetail(
                    step_id=step.id,
                    expected="the step to complete normally",
                    observed=f"outcome {outcome.name!r} fired: {outcome.message_template}",
                ),
            )

        budget = outcome.recovery.max_attempts if outcome.recovery else 0
        used = recovery_used.get(outcome.name, 0)
        if used >= budget:
            return self._failure(
                capability,
                state,
                step,
                FailureDetail(
                    step_id=step.id,
                    expected=f"the recovery to clear {outcome.name!r}",
                    observed=f"still firing after {used} of {budget} permitted attempt(s)",
                ),
            )
        recovery_used[outcome.name] = used + 1
        self._recover(outcome, state)
        state.recoveries.setdefault(step.id, []).append(outcome.name)
        state.drift.append(
            f"{step.id}: recovered from {outcome.name!r} (attempt {used + 1} of {budget})"
        )
        return None

    def _resolve(
        self, step: Step, observation: Observation, state: _RunState
    ) -> ActionTarget | None:
        if step.target is None:
            return None
        if observation.tree is None:
            raise LocatorExhausted(step.target, [Attempt("<no tree>", 0)])

        resolution: Resolution = self.resolver.resolve(observation.tree, step.target)
        state.locator_usage[step.id] = resolution.strategy
        state.attempts[step.id] = [attempt.strategy for attempt in resolution.attempts]
        if resolution.drift:
            state.drift.append(resolution.drift)
        return to_action_target(resolution.locator, step.target)

    def _act(
        self,
        step: Step,
        target: ActionTarget | None,
        params: dict[str, Any],
        verdict: PolicyVerdict,
    ) -> ActionResult:
        """Perform one gated action.

        The verdict is a parameter rather than something fetched here, so the chokepoint is
        visible in the signature: this cannot be called by a path that never passed C2.
        """
        if not verdict.allowed:  # pragma: no cover - the caller returns before this
            raise ReplayError(f"step {step.id!r} reached the surface without an allow verdict")
        action = Action(
            kind=_ACTIONS[step.action],
            target=target,
            value=resolve_value(step, params),
            timeout_ms=step.timeout_ms,
        )
        result = self.surface.act(action)
        self.logger.event(
            "replay_step",
            step=step.id,
            intent=step.intent,
            action=step.action,
            ok=result.ok,
            error=result.error,
            policy_rule=verdict.rule,
            url_after=result.url_after,
            duration_ms=result.duration_ms,
        )
        return result

    def _recover(self, outcome: Outcome, state: _RunState) -> None:
        """Apply a declared remedy. Recovery is itself an action, so it is gated too."""
        recovery = outcome.recovery
        if recovery is None:  # pragma: no cover - the schema forbids it
            return
        self.logger.event("replay_recovery", outcome=outcome.name, action=recovery.action)

        if recovery.action == "wait_and_retry":
            self._sleep(1.0)
            return
        if recovery.action == "retry_step":
            return
        if recovery.action == "dismiss_dialog" and recovery.target is not None:
            observation = self.surface.observe()
            if observation.tree is None:
                return
            resolution = self.resolver.resolve(observation.tree, recovery.target)
            target = to_action_target(resolution.locator, recovery.target)
            action = Action(kind="click", target=target)
            verdict = self.policy.check(
                action,
                PolicyContext(mode="replay", current_url=observation.url, step_recorded=True),
            )
            if verdict.allowed:
                self.surface.act(action)
            return
        state.drift.append(f"recovery {recovery.action!r} is not implemented")

    # -- assembling the result -----------------------------------------------------------

    def _outputs(self, capability: Capability, state: _RunState) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for spec in capability.outputs:
            raw = state.extracted.get(spec.source_step_id)
            if raw is None:
                continue
            outputs[spec.name] = _coerce(raw.strip(), spec.type, f"output {spec.name!r}")
        return outputs

    def _success(self, capability: Capability, state: _RunState) -> ReplayResult:
        outputs = self._outputs(capability, state)
        missing = [spec.name for spec in capability.outputs if spec.name not in outputs]
        if missing:
            return self._failure(
                capability,
                state,
                None,
                FailureDetail(
                    step_id=capability.steps[-1].id,
                    expected=f"declared outputs {[s.name for s in capability.outputs]}",
                    observed=f"never extracted {missing}",
                ),
            )
        return self._finish(capability, state, "success", outputs=outputs)

    def _business(self, capability: Capability, state: _RunState, outcome: Outcome) -> ReplayResult:
        """A declared business situation.

        This is a result the caller asked for, not an exception and not a failure. "No such
        member" is an answer. The outputs the capability said survive this outcome are
        returned with it; the rest are simply absent.
        """
        available = self._outputs(capability, state)
        partial = {
            name: value for name, value in available.items() if name in outcome.partial_outputs
        }
        self.logger.event(
            "replay_business_outcome",
            outcome=outcome.name,
            partial_outputs=sorted(partial),
            declared_partial=sorted(outcome.partial_outputs),
        )
        return self._finish(
            capability,
            state,
            "business_outcome",
            outputs=partial or None,
            outcome=OutcomeResult(
                name=outcome.name, kind=outcome.kind, message=outcome.message_template
            ),
        )

    def _capture_evidence(self, step_id: str | None) -> list[str]:
        """Freeze what the screen looked like when it went wrong.

        The accessibility snapshot is always written: it is the observation the run was
        actually making decisions from, it is small, and it carries no pixels. A screenshot
        is only taken when explicitly permitted, because a failure screenshot of a servicing
        screen is unredacted PII.
        """
        if step_id is None:
            # Nothing has been touched yet, so there is no screen worth freezing.
            return []
        paths: list[str] = []
        try:
            observation = self.surface.observe(screenshot=self.allow_screenshots)
        except Exception as exc:  # noqa: BLE001 - capturing evidence must never mask the failure
            self.logger.event("evidence_capture_failed", step=step_id, error=str(exc)[:200])
            return paths

        snapshot = {
            "url": observation.url,
            "title": observation.title,
            "observation_hash": observation.observation_hash,
            "tree": observation.tree.model_dump() if observation.tree else None,
        }
        paths.append(
            str(
                self.logger.save_artifact(
                    f"failure-{step_id}.ax.json",
                    json.dumps(snapshot, indent=2).encode("utf-8"),
                )
            )
        )
        if observation.screenshot is not None:
            paths.append(
                str(self.logger.save_artifact(f"failure-{step_id}.png", observation.screenshot))
            )
        self.logger.event("evidence_captured", step=step_id, paths=paths)
        return paths

    def _failure(
        self,
        capability: Capability,
        state: _RunState,
        step: Step | None,
        failure: FailureDetail,
    ) -> ReplayResult:
        """Every failure carries evidence and the candidates that were tried getting there."""
        step_id = step.id if step is not None else None
        detail = failure.model_copy(
            update={
                "evidence_paths": failure.evidence_paths or self._capture_evidence(step_id),
                "candidates_tried": (
                    failure.candidates_tried or state.attempts.get(step_id or "", [])
                ),
            }
        )
        return self._finish(capability, state, "failure", failure=detail)

    def _locator_failure(self, step: Step, attempts: list[Attempt]) -> FailureDetail:
        return FailureDetail(
            step_id=step.id,
            expected=f"a control matching {step.target.role if step.target else '?'}",
            observed="; ".join(attempt.describe() for attempt in attempts),
            candidates_tried=[attempt.strategy for attempt in attempts],
        )

    def _finish(
        self,
        capability: Capability,
        state: _RunState,
        status: str,
        *,
        outputs: dict[str, Any] | None = None,
        outcome: OutcomeResult | None = None,
        failure: FailureDetail | None = None,
    ) -> ReplayResult:
        result = ReplayResult(
            status=status,  # type: ignore[arg-type]
            capability_id=capability.id,
            capability_version=capability.version,
            run_id=self.logger.run_id,
            outputs=outputs,
            outcome=outcome,
            failure=failure,
            steps_executed=state.steps_executed,
            duration_ms=max(0, int((self._monotonic() - state.started) * 1000)),
            locator_usage=state.locator_usage,
            recoveries=state.recoveries,
            drift_signals=state.drift,
        )
        self.logger.event("replay_end", result=result)
        # The result belongs beside the log it explains: run.jsonl is how the replay went,
        # result.json is what the caller was told. Reading one without the other is guesswork.
        self.logger.save_artifact("result.json", result.model_dump_json(indent=2).encode("utf-8"))
        return result


# Step actions are the artifact's vocabulary; surface actions are the executor's. They line
# up one-to-one apart from `assert`, which checks a condition without touching the surface.
_ACTIONS: dict[str, Any] = {
    "navigate": "navigate",
    "click": "click",
    "type": "type",
    "select": "select",
    "press_key": "press_key",
    "wait_for": "wait_for",
    "extract": "extract",
    "assert": "wait_for",
}


def load_capability(capability_id: str, directory: Path = CAPABILITY_DIR) -> Capability:
    """Read a saved capability by id."""
    path = directory / f"{capability_id}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplayError(f"cannot read capability {capability_id!r} at {path}: {exc}") from exc
    return Capability.model_validate(json.loads(raw))


def save_capability(capability: Capability, directory: Path = CAPABILITY_DIR) -> Path:
    """Write a capability back over itself. Used when drift demotes one to draft."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{capability.id}.json"
    path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")
    return path
