"""Discovery loop: observe, decide, act until the goal is met or a stopping condition fires."""

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from cua.discovery.prompts import (
    SYSTEM,
    goal_message,
    refusal_message,
    render_observation,
    result_message,
)
from cua.discovery.tools import TOOL_SPECS, Finish, one_call, parse
from cua.evidence.logger import RunLogger
from cua.llm.base import LLMClient, LLMResponse, Message
from cua.policy.engine import PolicyContext, PolicyEngine, PolicyVerdict
from cua.recording.assemble import ActedStep, assemble
from cua.recording.synthesizer import SynthesisError
from cua.surfaces.base import Action, ActionResult, Observation, Surface

StopReason = Literal["goal_reached", "max_steps", "timeout", "no_progress", "escalation_required"]

# Free-tier TPM is the binding constraint, so the model sees the goal plus a recent window.
MAX_HISTORY_MESSAGES = 12

NO_CALL_NUDGE = (
    "You replied without calling a tool. Every turn must be exactly one tool call. "
    "If the goal is already met, call finish with the values you found. Otherwise call "
    "the single action that moves you closer."
)


class DiscoveryError(Exception):
    """The run could not start or could not continue. Not a stopping condition."""


class DiscoveryConfig(BaseModel):
    """Everything the loop needs that is not a collaborator."""

    goal: str
    target: str
    tenant: str = "tenant-a"
    max_steps: int = 25
    wall_clock_seconds: float = 300.0
    no_progress_limit: int = 3
    vendor_product: str = "unknown"
    """Product identifier recorded on the draft artifact.

    Nothing on a page reliably says which product it is, so this is supplied rather than
    guessed. It defaults to "unknown", which is at least honest.
    """
    screenshots: bool = False
    """Capture a screenshot per observation into the evidence directory.

    Whether it also reaches the model is decided by the provider, not by this flag.
    """


class DiscoveryResult(BaseModel):
    """What the run produced, and why it stopped."""

    run_id: str
    goal: str
    stop_reason: StopReason
    steps: int
    duration_ms: int
    evidence_path: Path
    summary: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    capability_path: Path | None = Field(
        default=None,
        description="The draft artifact this run produced, written beside its evidence.",
    )


class DiscoveryRunner:
    """observe -> decide -> act, with a policy check between every decision and every act."""

    def __init__(
        self,
        *,
        surface: Surface,
        llm: LLMClient,
        policy: PolicyEngine,
        logger: RunLogger,
        config: DiscoveryConfig,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.policy = policy
        self.logger = logger
        self.config = config
        self._monotonic = monotonic
        self._history: list[Message] = []
        #: What was acted on, and the tree it was decided from. This is the raw material
        #: the artifact is assembled from at the end of a successful run.
        self._acted: list[ActedStep] = []

    def run(self) -> DiscoveryResult:
        started = self._monotonic()
        self.logger.event(
            "discovery_start",
            goal=self.config.goal,
            target=self.config.target,
            tenant=self.config.tenant,
            max_steps=self.config.max_steps,
        )
        self._history = [
            Message(
                role="user",
                content=goal_message(self.config.goal, self.config.target, self.config.tenant),
            )
        ]
        self._open_target()

        steps = 0
        summary: str | None = None
        outputs: dict[str, str] = {}
        last_hash: str | None = None
        last_action: str | None = None
        repeats = 0
        stop: StopReason = "max_steps"

        while True:
            if steps >= self.config.max_steps:
                stop = "max_steps"
                break
            if self._monotonic() - started >= self.config.wall_clock_seconds:
                stop = "timeout"
                break

            observation = self.surface.observe(screenshot=self.config.screenshots)
            self._save_observation(observation, steps + 1)
            # A read is not a lack of progress. `extract` deliberately leaves the page where
            # it was, so counting its unchanged observation makes any capability that reads
            # two values off one screen undiscoverable - the loop stops before it can finish.
            # Only actions that were supposed to move the page count towards being stuck.
            if last_action == "extract":
                last_hash = observation.observation_hash
            else:
                repeats = repeats + 1 if observation.observation_hash == last_hash else 1
                last_hash = observation.observation_hash
            if repeats >= self.config.no_progress_limit:
                self.logger.event(
                    "discovery_no_progress",
                    observation_hash=observation.observation_hash,
                    repeats=repeats,
                )
                stop = "no_progress"
                break

            steps += 1
            step_started = self._monotonic()
            decision, reasoning = self._decide(observation, steps)

            if isinstance(decision, Finish):
                summary, outputs = decision.summary, decision.outputs
                self._record(
                    steps, observation, reasoning, {"tool": "finish"}, None, None, step_started
                )
                stop = "goal_reached"
                break

            verdict, result = self._execute(decision, observation)
            last_action = decision.kind
            self._record(steps, observation, reasoning, decision, verdict, result, step_started)
            if result is not None and result.ok and observation.tree is not None:
                self._acted.append(
                    ActedStep(tree=observation.tree, action=decision, extracted=result.extracted)
                )

            if verdict.decision == "block_and_escalate":
                stop = "escalation_required"
                break
            self._remember(reasoning, verdict, result)

        artifact = self._emit_capability(stop, outputs) if stop == "goal_reached" else None

        duration_ms = int((self._monotonic() - started) * 1000)
        self.logger.event(
            "discovery_end",
            stop_reason=stop,
            steps=steps,
            duration_ms=duration_ms,
            summary=summary,
            outputs=outputs,
            capability_path=artifact,
        )
        return DiscoveryResult(
            run_id=self.logger.run_id,
            goal=self.config.goal,
            stop_reason=stop,
            steps=steps,
            duration_ms=duration_ms,
            evidence_path=self.logger.path,
            summary=summary,
            outputs=outputs,
            capability_path=artifact,
        )

    # ---- the artifact --------------------------------------------------------

    def _emit_capability(self, stop: str, outputs: dict[str, str]) -> Path | None:
        """Write the draft artifact beside the run that produced it (PRD 10).

        A failure here must not lose the run: the evidence is already on disk and is worth
        more than the draft. So this reports and returns None rather than raising.
        """
        try:
            capability = assemble(
                goal=self.config.goal,
                run_id=self.logger.run_id,
                model=getattr(self.llm, "model", "unknown"),
                entry_url=self.config.target,
                acted=self._acted,
                tenant_id=self.config.tenant,
                outputs=outputs,
                vendor_product=self.config.vendor_product,
            )
        except (ValueError, SynthesisError) as exc:
            self.logger.event("capability_not_emitted", why=str(exc), stop_reason=stop)
            return None
        path = self.logger.save_artifact(
            "capability.json", capability.model_dump_json(indent=2).encode("utf-8")
        )
        self.logger.event(
            "capability_emitted",
            capability_id=capability.id,
            steps=len(capability.steps),
            outputs=[o.name for o in capability.outputs],
            state=capability.provenance.state,
            path=str(path),
        )
        return path

    # ---- one iteration -------------------------------------------------------

    def _decide(self, observation: Observation, step: int) -> tuple[Action | Finish, str | None]:
        """Ask the model for exactly one action from the closed schema.

        An invented tool or several calls at once is a hard error. A turn with *no* call is
        the one recoverable case: the model reasoned and forgot to act, which is a lapse
        rather than a challenge to the schema, so it gets exactly one nudge.
        """
        self._history.append(Message(role="user", content=render_observation(observation, step)))
        response = self._complete(observation)

        if not response.tool_calls:
            self.logger.event("discovery_nudge", step=step, said=response.text)
            if response.text:
                self._history.append(Message(role="assistant", content=response.text))
            self._history.append(Message(role="user", content=NO_CALL_NUDGE))
            response = self._complete(observation)

        # A call outside the schema raises: the loop does not negotiate with the model.
        return parse(one_call(response.tool_calls)), response.text

    def _complete(self, observation: Observation) -> LLMResponse:
        """One provider call. Images go only to a provider that can read them."""
        shot = observation.screenshot
        return self.llm.complete(
            [Message(role="system", content=SYSTEM), *self._window()],
            tools=list(TOOL_SPECS),
            images=[shot] if shot is not None and self.llm.supports_images else None,
        )

    def _execute(
        self, action: Action, observation: Observation
    ) -> tuple[PolicyVerdict, ActionResult | None]:
        """The chokepoint (C2). No action reaches the surface without a verdict."""
        verdict = self.policy.check(
            action, PolicyContext(mode="discovery", current_url=observation.url)
        )
        if not verdict.allowed:
            return verdict, None
        return verdict, self.surface.act(action)

    def _open_target(self) -> None:
        """Navigate to the entry point, gated like any other action."""
        action = Action(kind="navigate", value=self.config.target)
        verdict = self.policy.check(action, PolicyContext(mode="discovery"))
        if not verdict.allowed:
            raise DiscoveryError(
                f"policy refused the entry point {self.config.target!r} "
                f"(rule={verdict.rule}): {verdict.reason}"
            )
        result = self.surface.act(action)
        self.logger.event("discovery_entry", action=action, verdict=verdict, result=result)
        if not result.ok:
            raise DiscoveryError(f"could not open {self.config.target!r}: {result.error}")

    def _save_observation(self, observation: Observation, step: int) -> None:
        """One observation on disk: the tree always, the pixels only if they were captured.

        The AX snapshot is the one that matters. It is what the model actually reasoned over,
        so a reviewer reconstructing a decision needs it - and unlike a screenshot it costs
        nothing to share, because it holds no pixels of a servicing screen.
        """
        self.logger.save_artifact(
            f"step-{step:02d}.json",
            observation.model_dump_json(indent=2).encode("utf-8"),
            subdir="ax-snapshots",
        )
        if observation.screenshot is None:
            return
        path = self.logger.save_artifact(
            f"step-{step:02d}.png", observation.screenshot, subdir="screenshots"
        )
        self.logger.event(
            "screenshot_captured",
            step=step,
            path=path,
            sent_to_model=self.llm.supports_images,
        )

    # ---- bookkeeping ---------------------------------------------------------

    def _window(self) -> list[Message]:
        """Goal first, then the most recent turns. The middle is what falls off."""
        if len(self._history) <= MAX_HISTORY_MESSAGES:
            return self._history
        return [self._history[0], *self._history[-(MAX_HISTORY_MESSAGES - 1) :]]

    def _remember(
        self, reasoning: str | None, verdict: PolicyVerdict, result: ActionResult | None
    ) -> None:
        if reasoning:
            self._history.append(Message(role="assistant", content=reasoning))
        if result is None:
            self._history.append(
                Message(role="user", content=refusal_message(verdict.rule, verdict.reason))
            )
        else:
            self._history.append(
                Message(
                    role="user", content=result_message(result.ok, result.error, result.extracted)
                )
            )

    def _record(
        self,
        step: int,
        observation: Observation,
        reasoning: str | None,
        action: Action | dict[str, str],
        verdict: PolicyVerdict | None,
        result: ActionResult | None,
        step_started: float,
    ) -> None:
        self.logger.event(
            "discovery_step",
            step=step,
            observation_hash=observation.observation_hash,
            pruning_ratio=round(observation.pruning.ratio, 3),
            nodes_before=observation.pruning.nodes_before,
            nodes_after=observation.pruning.nodes_after,
            reasoning=reasoning,
            action=action,
            verdict=verdict,
            result=result,
            elapsed_ms=int((self._monotonic() - step_started) * 1000),
        )


def new_run_id() -> str:
    """Sortable, unique, and safe as a directory name."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
