"""Pydantic models for every persisted artifact: Capability, Step, ControlDescriptor, Outcome.

This module is the centre of the project. A capability is what discovery produces and what
replay consumes, so these types are the contract between a model-driven recording and a
deterministic execution that never calls a model.

Every field carries a description because `export.py` turns these models into the JSON Schema
an agent reads to decide whether and how to call a capability. A field without a description
is a field the caller has to guess at.
"""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

# ---- vocabularies -----------------------------------------------------------------------

LocatorStrategy = Literal[
    "role_name", "anchor_relative", "region_ordinal", "text_content", "dom_hint"
]
StepAction = Literal[
    "navigate", "click", "type", "select", "press_key", "wait_for", "extract", "assert"
]
RiskClass = Literal["safe", "risky", "irreversible"]
ConditionKind = Literal[
    "control_present", "control_absent", "text_present", "url_matches", "value_equals"
]
OutcomeKind = Literal["business", "recoverable", "hard_failure"]
RecoveryAction = Literal["dismiss_dialog", "retry_step", "wait_and_retry", "reauthenticate"]
ParameterType = Literal["string", "integer", "number", "boolean", "date"]
ApprovalState = Literal["draft", "approved"]
ReplayStatus = Literal["success", "business_outcome", "failure"]

# A surface-specific locator is a terminal fallback and is never scored above this.
SURFACE_SPECIFIC_SCORE_CAP = 0.3

SCHEMA_VERSION = "1.0"


# ---- locating a control -----------------------------------------------------------------


class Locator(BaseModel):
    """One way to find a control, with an honest estimate of how well it will age.

    A locator is a candidate, never the answer. `ControlDescriptor` holds several and the
    resolver tries them in order, so a single brittle strategy failing is survivable.
    """

    strategy: LocatorStrategy = Field(
        description=(
            "How to find the control. role_name uses the accessibility role and name; "
            "anchor_relative locates it by its relationship to nearby text and is the "
            "primary strategy on legacy surfaces; region_ordinal falls back to position "
            "within a named region; text_content matches visible text and is brittle to "
            "localisation; dom_hint is a css or xpath selector and a terminal fallback."
        )
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Strategy-specific arguments. role_name: role, name, match. anchor_relative: "
            "anchor_text, anchor_role, relation (same_row|following|within_region), "
            "target_role, index. region_ordinal: region, role, index. text_content: text, "
            "match. dom_hint: css or xpath."
        ),
    )
    stability_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Heuristic 0-1 estimate of how well this candidate survives UI change. Scored, "
            "not measured: named and anchor-based candidates score higher, index-dependent "
            "and generated-id-dependent ones lower, and surface-specific ones are capped."
        ),
    )
    verified_unique_at_record: bool = Field(
        description=(
            "Whether this candidate was re-resolved against the live page at record time "
            "and matched exactly one node. A false here means the candidate was kept "
            "without proof and should not be trusted as a primary."
        )
    )
    surface_specific: bool = Field(
        default=False,
        description=(
            "Whether this candidate only works on the surface it was recorded against. "
            "True for dom_hint and nothing else. A non-web resolver skips these entirely, "
            "which is what keeps the artifact portable (C3)."
        ),
    )

    @model_validator(mode="after")
    def _enforce_surface_specificity(self) -> Self:
        """dom_hint is surface-specific and capped; every other strategy is neither.

        This is an invariant rather than a convention: if a css selector could pass itself
        off as a portable candidate, the desktop story would be a claim instead of a seam.
        """
        object.__setattr__(self, "surface_specific", self.strategy == "dom_hint")
        if self.surface_specific and self.stability_score > SURFACE_SPECIFIC_SCORE_CAP:
            object.__setattr__(self, "stability_score", SURFACE_SPECIFIC_SCORE_CAP)
        return self


class ControlDescriptor(BaseModel):
    """A control, described by what it is plus every way we know to find it.

    The candidate list is the point. Recording one locator per control produces an artifact
    that breaks on the first UI change; recording a ranked chain produces one that degrades.
    """

    role: str = Field(
        description="Accessibility role of the control: button, textbox, link, cell, combobox."
    )
    name: str | None = Field(
        default=None,
        description="Accessible name of the control, if it has one. None where nothing is named.",
    )
    candidates: list[Locator] = Field(
        min_length=1,
        description=(
            "Ranked locator candidates, strongest first. The resolver walks them in order "
            "and records which one actually fired, so a non-primary candidate firing is a "
            "drift signal. Ordering is maintained by the model, not by the caller."
        ),
    )

    @model_validator(mode="after")
    def _rank_candidates(self) -> Self:
        """Ranked is an invariant. Candidates are sorted by score, so index 0 is the primary."""
        object.__setattr__(
            self, "candidates", sorted(self.candidates, key=lambda c: -c.stability_score)
        )
        return self

    @property
    def primary(self) -> Locator:
        """The strongest candidate. Anything else firing means the surface has moved."""
        return self.candidates[0]

    @property
    def portable_candidates(self) -> list[Locator]:
        """Candidates a non-web surface can attempt, i.e. everything but dom_hint."""
        return [c for c in self.candidates if not c.surface_specific]


# ---- asserting things about a page ------------------------------------------------------


class Condition(BaseModel):
    """A testable claim about the current screen.

    One type serves two jobs: as a step's checkpoint it asserts the step worked, and as an
    outcome's detector it recognises a situation. They are the same question asked for
    different reasons, so they are the same type.
    """

    kind: ConditionKind = Field(
        description=(
            "What is being checked: control_present, control_absent, text_present, "
            "url_matches, or value_equals."
        )
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments for the check. control_present/absent: role, name. text_present: "
            "text. url_matches: pattern. value_equals: role, name, value."
        ),
    )
    negate: bool = Field(default=False, description="Whether to invert the result of the check.")


# ---- the error taxonomy -----------------------------------------------------------------


class Recovery(BaseModel):
    """What to do about a recoverable outcome, and how many times to try it."""

    action: RecoveryAction = Field(
        description=(
            "The remedy: dismiss_dialog closes a known interstitial, retry_step repeats the "
            "step, wait_and_retry pauses first, reauthenticate re-establishes the session."
        )
    )
    max_attempts: int = Field(
        ge=1,
        le=5,
        description="How many times the remedy may be applied before this becomes a failure.",
    )
    target: ControlDescriptor | None = Field(
        default=None,
        description="The control the remedy acts on, where it needs one (a dialog's close button).",
    )


class Outcome(BaseModel):
    """A situation the capability knows how to recognise, and what it means.

    `kind` is the load-bearing type in this project. The brief names conflating a business
    answer with a system failure as the most common design mistake, so the distinction lives
    in the type system rather than in a convention:

    - **business** - a legitimate answer the caller asked for. "No such member" is a result,
      not an exception. Returns status=business_outcome.
    - **recoverable** - a condition replay handles and continues past: a known interstitial,
      one bounded retry on a transient load.
    - **hard_failure** - stop, capture evidence, return something debuggable. Unknown page
      state, a checkpoint that failed with no matching detector, an exhausted locator chain.
    """

    name: str = Field(
        description="Stable identifier for this outcome, e.g. member_not_found.",
    )
    kind: OutcomeKind = Field(
        description=(
            "business: a legitimate answer for the caller, returned as a result rather than "
            "an error. recoverable: handled and continued past. hard_failure: stop and "
            "surface a debuggable error."
        )
    )
    detect: Condition = Field(description="The condition that identifies this outcome on screen.")
    applies_to: list[str] | Literal["any"] = Field(
        default="any",
        description=(
            "Step ids this outcome can occur on, or 'any' when it can happen anywhere in "
            "the flow, as a session timeout can."
        ),
    )
    recovery: Recovery | None = Field(
        default=None,
        description="How to recover. Permitted only when kind is recoverable.",
    )
    message_template: str = Field(
        description="Human-readable explanation, returned to the caller with the result."
    )
    partial_outputs: list[str] = Field(
        default_factory=list,
        description=(
            "Names of declared outputs that are still valid when this outcome fires. A "
            "business outcome often carries real data even though the flow stopped early."
        ),
    )

    @model_validator(mode="after")
    def _recovery_only_when_recoverable(self) -> Self:
        if self.recovery is not None and self.kind != "recoverable":
            raise ValueError(f"outcome {self.name!r} is {self.kind} and cannot carry a recovery")
        if self.recovery is None and self.kind == "recoverable":
            raise ValueError(f"outcome {self.name!r} is recoverable but declares no recovery")
        return self


# ---- inputs and outputs -----------------------------------------------------------------


class Parameter(BaseModel):
    """An input the caller supplies when invoking the capability.

    `sensitive` is a persistence control, not a display hint. A sensitive value is supplied
    per invocation and must never reach an artifact, a log, an evidence file or a stability
    record. The model enforces the part it can: a sensitive parameter cannot carry an example.
    """

    name: str = Field(description="Parameter name, as used in step values via a ParamRef.")
    type: ParameterType = Field(
        description="Value type: string, integer, number, boolean or date (ISO 8601)."
    )
    required: bool = Field(description="Whether the caller must supply this parameter.")
    sensitive: bool = Field(
        default=False,
        description=(
            "Whether the value is sensitive. A sensitive value is never persisted to the "
            "artifact, the run log, evidence or the stability record. It is supplied per "
            "invocation and dropped afterwards."
        ),
    )
    description: str = Field(
        description="What this parameter means, read by an agent deciding how to call this."
    )
    example: str | None = Field(
        default=None,
        description=(
            "A sample value, to show an agent the expected shape. Always null for a "
            "sensitive parameter: an example of a real account number is a real account number."
        ),
    )

    @model_validator(mode="after")
    def _no_example_for_sensitive(self) -> Self:
        if self.sensitive and self.example is not None:
            object.__setattr__(self, "example", None)
        return self


class OutputSpec(BaseModel):
    """A value the capability promises to return, and where in the flow it comes from."""

    name: str = Field(description="Output name, as it appears in the result's outputs map.")
    type: ParameterType = Field(description="Value type of the extracted output.")
    source_step_id: str = Field(description="Id of the step whose extraction produces this output.")
    description: str = Field(description="What this output means to the caller.")
    sensitive: bool = Field(
        default=False,
        description=(
            "Whether the value is sensitive. Sensitive outputs are returned to the caller "
            "but never written to evidence or logs."
        ),
    )


class ParamRef(BaseModel):
    """A step value that comes from a caller-supplied parameter rather than a constant.

    Sensitive values reach a step only through one of these, which is what keeps them out
    of the artifact: the artifact records the reference, never the value.
    """

    param: str = Field(description="Name of the parameter whose value to use.")


# ---- the flow ----------------------------------------------------------------------------


class Step(BaseModel):
    """One action in the recorded flow, with the check that proves it worked."""

    id: str = Field(description="Stable identifier for this step, referenced by outcomes.")
    intent: str = Field(
        description="What this step is for, in human terms: 'search for the member'."
    )
    action: StepAction = Field(
        description=(
            "The action to perform: navigate, click, type, select, press_key, wait_for, "
            "extract or assert."
        )
    )
    target: ControlDescriptor | None = Field(
        default=None,
        description="The control acted on. None for actions that need no control, like navigate.",
    )
    value: str | ParamRef | None = Field(
        default=None,
        description=(
            "The value for the action: a literal string, or a ParamRef naming a parameter "
            "supplied at invocation. Sensitive values are always references, never literals."
        ),
    )
    checkpoint: Condition | None = Field(
        default=None,
        description=(
            "Condition asserted after the action. A checkpoint that fails with no matching "
            "outcome detector is a hard failure."
        ),
    )
    timeout_ms: int = Field(
        default=10_000, gt=0, description="How long to wait for this step before giving up."
    )
    risk_class: RiskClass = Field(
        default="safe",
        description=(
            "Risk of this step, gated by the policy engine. safe: reads and navigation. "
            "risky: submits, saves, creates. irreversible: deletes, transfers, disbursements "
            "- always blocked and escalated to a human, never model-approved."
        ),
    )


# ---- what the capability belongs to ------------------------------------------------------


class AppRef(BaseModel):
    """The application this capability was recorded against."""

    vendor_product: str = Field(
        description="Product identifier, e.g. acme-core-servicing or dolibarr."
    )
    product_version: str | None = Field(
        default=None, description="Product version observed at record time, if detectable."
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant this capability is specialised for. None means it is the base.",
    )
    base_capability_id: str | None = Field(
        default=None,
        description="For a tenant specialisation, the id of the base capability it derives from.",
    )


class EntryPoint(BaseModel):
    """Where the flow starts, so replay can reach the same screen discovery began on."""

    url: str = Field(description="Absolute url the flow starts from.")
    requires_authenticated_session: bool = Field(
        default=True,
        description=(
            "Whether an authenticated session must already exist. Authentication is out of "
            "scope: replay attaches to a session that is already signed in."
        ),
    )


class CapabilityPolicy(BaseModel):
    """Per-capability safety limits, layered on top of the global policy file."""

    allowed_hosts: list[str] = Field(
        default_factory=list,
        description=(
            "Host patterns this capability may reach. Empty means inherit the global policy."
        ),
    )
    allowed_routes: list[str] = Field(
        default_factory=list,
        description=(
            "Route patterns this capability may reach. Empty means inherit the global policy."
        ),
    )
    max_steps: int = Field(
        default=25, gt=0, description="Ceiling on steps executed during one replay."
    )
    max_wall_clock_seconds: int = Field(
        default=300, gt=0, description="Ceiling on wall-clock time for one replay."
    )


class StabilityRecord(BaseModel):
    """Measured evidence that replay is deterministic, rather than a claim that it is."""

    runs: int = Field(ge=0, description="How many replays this record summarises.")
    passes: int = Field(ge=0, description="How many of those replays succeeded.")
    measured_at: datetime = Field(description="When the measurement was taken.")
    locator_usage: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description=(
            "Per control, how often each locator strategy actually fired. A non-primary "
            "strategy appearing here is the earliest visible sign of UI drift."
        ),
    )
    recoveries: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per step, how many runs needed a recovery. This is the baseline that makes "
            "'a recoverable outcome that never fired before' answerable rather than guessed."
        ),
    )
    drift_signals: list[str] = Field(
        default_factory=list,
        description=(
            "Observations suggesting the surface has moved: a fallback candidate firing, or "
            "a recoverable outcome triggering on a step that never needed it before."
        ),
    )

    @property
    def pass_rate(self) -> float:
        """Fraction of replays that succeeded. 0.0 when nothing has been measured."""
        return self.passes / self.runs if self.runs else 0.0


class Provenance(BaseModel):
    """Where this capability came from and whether a human has signed it off.

    Unattended replay requires state='approved' and outcomes_reviewed=True. A model
    proposes outcomes; it does not get to approve its own error taxonomy.
    """

    discovered_at: datetime = Field(description="When the discovery run that produced this ran.")
    model: str = Field(description="Model identifier that drove the discovery run.")
    discovery_run_id: str = Field(
        description="Id of the discovery run, linking this artifact to its evidence on disk."
    )
    state: ApprovalState = Field(
        default="draft",
        description=(
            "draft until a human reviews it; approved once signed off. Drift can demote an "
            "approved capability back to draft."
        ),
    )
    approved_by: str | None = Field(
        default=None, description="Who approved it. Null while the state is draft."
    )
    outcomes_reviewed: bool = Field(
        default=False,
        description=(
            "Whether a human has reviewed the proposed outcomes. Required for unattended "
            "replay: the error taxonomy is the part a model is least trustworthy about."
        ),
    )

    @property
    def replayable_unattended(self) -> bool:
        """Both gates, together. Either one alone is not enough."""
        return self.state == "approved" and self.outcomes_reviewed


# ---- the artifact -------------------------------------------------------------------------


class Capability(BaseModel):
    """A reusable, typed, versioned recording of how to accomplish one goal in a UI.

    This is what the whole system exists to produce. Discovery writes one; replay executes
    it with no model in the decision loop; the catalog exposes it to an agent as a callable
    tool. Its JSON Schema, exported by `export.py`, is the agent-facing contract.
    """

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Version of this artifact format, for forward compatibility.",
    )
    id: str = Field(description="Stable dotted identifier, e.g. member.savings_balance.lookup.")
    name: str = Field(description="Short human-readable name.")
    description: str = Field(
        description=(
            "What this capability does, written for an agent deciding whether to call it. "
            "This is the text a model reads when choosing between capabilities."
        )
    )
    version: str = Field(description="Semantic version, bumped whenever the steps change.")
    app: AppRef = Field(description="The application and tenant this was recorded against.")
    surface_kind: Literal["web", "desktop"] = Field(
        default="web", description="Kind of surface this capability drives."
    )
    entry: EntryPoint = Field(description="Where the flow starts.")
    parameters: list[Parameter] = Field(
        default_factory=list, description="Inputs the caller supplies at invocation."
    )
    outputs: list[OutputSpec] = Field(
        default_factory=list, description="Values the capability returns on success."
    )
    steps: list[Step] = Field(min_length=1, description="The recorded flow, in order.")
    outcomes: list[Outcome] = Field(
        default_factory=list,
        description="Situations this capability recognises, and what each one means.",
    )
    policy: CapabilityPolicy = Field(
        default_factory=CapabilityPolicy, description="Per-capability safety limits."
    )
    provenance: Provenance = Field(description="Origin and approval state.")
    stability: StabilityRecord | None = Field(
        default=None, description="Measured replay stability, once it has been measured."
    )

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        """A capability that references a step or parameter it does not declare is broken."""
        step_ids = {step.id for step in self.steps}
        if len(step_ids) != len(self.steps):
            raise ValueError("step ids must be unique")

        parameter_names = {parameter.name for parameter in self.parameters}
        for step in self.steps:
            if isinstance(step.value, ParamRef) and step.value.param not in parameter_names:
                raise ValueError(
                    f"step {step.id!r} references unknown parameter {step.value.param!r}"
                )

        output_names = {output.name for output in self.outputs}
        for output in self.outputs:
            if output.source_step_id not in step_ids:
                raise ValueError(
                    f"output {output.name!r} names unknown source step {output.source_step_id!r}"
                )
        for outcome in self.outcomes:
            if outcome.applies_to != "any":
                unknown = set(outcome.applies_to) - step_ids
                if unknown:
                    raise ValueError(
                        f"outcome {outcome.name!r} names unknown steps {sorted(unknown)}"
                    )
            missing = set(outcome.partial_outputs) - output_names
            if missing:
                raise ValueError(
                    f"outcome {outcome.name!r} names unknown outputs {sorted(missing)}"
                )
        return self

    @property
    def sensitive_parameters(self) -> list[str]:
        """Names of parameters whose values must never be persisted anywhere."""
        return [p.name for p in self.parameters if p.sensitive]


# ---- tenant specialisation -----------------------------------------------------------------


class Override(BaseModel):
    """One field of one step, replaced for a tenant."""

    step_id: str = Field(description="Id of the step this override applies to.")
    field_path: str = Field(
        description=(
            "Dotted path to the field within the step, e.g. target.candidates.0.params.name."
        )
    )
    value: Any = Field(description="Replacement value for that field.")


class CapabilityOverlay(BaseModel):
    """A tenant's differences from a base capability, kept additive and auditable.

    Reuse is the default and specialisation is a diff. Copying a capability per tenant would
    work once and rot immediately; an overlay states only what differs and can be checked
    against the base version it was verified for.
    """

    base_capability_id: str = Field(description="Id of the capability this overlay specialises.")
    base_version: str = Field(description="Version of the base this overlay was written against.")
    tenant_id: str = Field(description="Tenant this overlay applies to.")
    overrides: list[Override] = Field(
        default_factory=list, description="Field-level replacements, applied by path."
    )
    added_outcomes: list[Outcome] = Field(
        default_factory=list,
        description="Outcomes that exist only for this tenant, added to the base's set.",
    )
    verified_against: str = Field(
        description=(
            "The base version this overlay was last confirmed compatible with. If the base "
            "has moved past it, the overlay needs review and unattended replay is refused."
        )
    )

    def needs_review(self, current_base_version: str) -> bool:
        """Whether the base has moved since this overlay was last verified."""
        return current_base_version != self.verified_against


# ---- what a caller gets back -----------------------------------------------------------------


class OutcomeResult(BaseModel):
    """The outcome that fired during a replay, reported to the caller."""

    name: str = Field(description="Name of the outcome that fired.")
    kind: OutcomeKind = Field(description="Its class: business, recoverable or hard_failure.")
    message: str = Field(description="Rendered explanation for the caller.")


class FailureDetail(BaseModel):
    """Everything needed to debug a failed replay without re-running it."""

    step_id: str = Field(description="Step that failed.")
    expected: str = Field(description="What the step expected to be true afterwards.")
    observed: str = Field(description="What was actually observed instead.")
    candidates_tried: list[str] = Field(
        default_factory=list,
        description="Locator strategies attempted for the control, in the order they were tried.",
    )
    evidence_paths: list[str] = Field(
        default_factory=list,
        description="Paths to evidence captured at the point of failure.",
    )


class ReplayResult(BaseModel):
    """The contract a calling agent sees.

    The status field is the reason this type exists: a caller can tell "this member does not
    exist" from "the automation broke" without parsing a message string.
    """

    status: ReplayStatus = Field(
        description=(
            "success: the flow completed and outputs are present. business_outcome: a "
            "declared business situation occurred and is described in outcome. failure: "
            "the automation could not complete, described in failure."
        )
    )
    capability_id: str = Field(description="Id of the capability that was replayed.")
    capability_version: str = Field(description="Version of the capability that was replayed.")
    run_id: str = Field(description="Id of this replay run, linking it to evidence on disk.")
    outputs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Declared outputs, present on success and possibly partial on a business outcome."
        ),
    )
    outcome: OutcomeResult | None = Field(
        default=None, description="The outcome that fired, when one did."
    )
    failure: FailureDetail | None = Field(
        default=None, description="Debuggable detail, present only when status is failure."
    )
    steps_executed: int = Field(ge=0, description="How many steps ran before the result.")
    duration_ms: int = Field(ge=0, description="Wall-clock duration of the replay.")
    locator_usage: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per control, which locator strategy actually fired. A non-primary strategy "
            "here is a drift signal, and it costs nothing to record."
        ),
    )
    recoveries: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per step, the recoverable outcomes that fired and were recovered from. Recorded "
            "structurally rather than only as a log line, because drift detection has to ask "
            "whether a step needed a recovery it never needed before."
        ),
    )
    drift_signals: list[str] = Field(
        default_factory=list,
        description="Signals that the surface has moved since this capability was recorded.",
    )

    @model_validator(mode="after")
    def _detail_matches_status(self) -> Self:
        if self.status == "failure" and self.failure is None:
            raise ValueError("a failure result must carry failure detail")
        if self.status == "business_outcome" and self.outcome is None:
            raise ValueError("a business_outcome result must name the outcome that fired")
        return self
