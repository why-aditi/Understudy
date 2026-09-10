# PRD — Computer-Use Automation System

**Project:** interface.ai engineering take-home
**Owner:** Aditi Kala
**Status:** Draft for review
**Budget:** 11 evening sessions (~30–35 hours)

---

## 1. Purpose and success criteria

This is a hiring artifact, not a product. It is graded by humans reading `REPORT.md` and spot-checking that the code backs the claims in it.

Success means:

1. A complete vertical slice touching all six core requirements, none of them a TODO.
2. Every claim in the report is verifiable from `/evidence/` or by running one command.
3. Every decision is defensible in a follow-up interview.

Explicit non-goals: feature breadth, framework variety, scaling infrastructure, polish.

**Primary risk:** the only un-fakeable requirement is a real LLM-driven run against a live surface. It is scheduled first for that reason.

---

## 2. Scope

### Built for real

| Area | Built |
|---|---|
| Discovery | LLM observe→decide→act loop against a live web surface |
| Artifact | Typed, versioned Pydantic capability schema with JSON Schema export |
| Replay | Deterministic executor, no model in the decision loop |
| Errors | Three-class outcome taxonomy with declared detectors |
| Safety | Allowlist + risk classification enforced at the executor chokepoint |
| Escalation | Control lock, intervention record, human drives the same live session, resume |
| Evidence | Structured JSONL logs, AX snapshots, discovery + replay + failing-replay runs |
| Tenants | Base capability + tenant overlay, demonstrated across two app variants |
| Stability | Replay ×10 with locator-candidate usage distribution |
| Catalog | Saved capabilities exposed as callable typed tools |

### Deliberately mocked at a clean seam

| Area | What exists | What is mocked |
|---|---|---|
| Operator console | Intervention record, control lock, resume signal, human action capture | The UI itself — CLI + a static HTML page reading the intervention file |
| Desktop surface | `Surface` interface, `ControlDescriptor` designed surface-agnostic, one AX-tree dump proof against a native app | No `DesktopSurface` implementation |
| Multi-tenant | Overlay resolution across two variants | No tenant registry, no per-tenant config service |

### Out of scope entirely

Authentication and SSO flows, queueing or distributed execution, real co-browsing transport, OCR-based screenshot redaction, automatic recovery from UI drift, vision-only surfaces (canvas apps).

---

## 3. Target surface

**Discovery target:** an existing public multi-step application not built by us — search → detail → action, or a multi-field form with a confirmation step. This matters: automating an app you designed is grading your own exam, and a reviewer will discount the robustness section if the only surface is self-built.

**Fault-injection harness:** a small local app used *only* to produce error and tenant evidence. It is not a legacy simulation. It needs:

- a member search → detail → open sub-account → confirmation flow
- query-param flags: `?fail=not_found`, `?fail=permission`, `?fail=timeout`, `?fail=modal`, `?fail=slow`
- table-based layout, framesets on at least one screen, generated per-render IDs, no test IDs
- a second variant (`tenant-b`) with different branding, two renamed labels, one reordered column

**README must state:** the automation receives no privileged hooks, no injected IDs, no special endpoints. It drives the same markup a human operator sees.

---

## 4. Architecture

```
                 ┌────────────────────────────┐
   goal + target │        Discovery Run       │
   ──────────────▶  LLM loop (observe/decide) │
                 └─────────────┬──────────────┘
                               │ actions
                               ▼
                 ┌────────────────────────────┐
                 │      PolicyEngine          │  ← allowlist, risk class
                 │   (single chokepoint)      │     BLOCKS or ESCALATES
                 └─────────────┬──────────────┘
                               ▼
                 ┌────────────────────────────┐
                 │        Surface             │  observe() → Observation
                 │  WebSurface | (Desktop)    │  act(Action) → ActionResult
                 └─────────────┬──────────────┘
                               ▼
                 ┌────────────────────────────┐
                 │   SessionRegistry          │  long-lived browser
                 │   + ControlLock            │  holder: automation|human
                 └────────────────────────────┘

   Discovery ──▶ LocatorSynthesizer ──▶ Capability (artifact)
                                              │
   inputs ──────────────────────────────▶ ReplayEngine ──▶ ReplayResult
                                              │
                                        InterventionRequest ──▶ human
```

### Key architectural constraints

**C1 — the session outlives the run.** Requirement 3.6 says a human takes control of *the same live session*. A script that opens and closes its own browser cannot satisfy this at any price. Runs attach to a session from the registry; they never own the browser.

**C2 — one action chokepoint.** Every action from every source passes `PolicyEngine.check()` before reaching a surface. Prompt-level guardrails are not guardrails.

**C3 — the artifact never contains surface-specific locators as a primary strategy.** DOM hints are permitted only as a terminal fallback, flagged `surface_specific: true`, and skipped by any non-web resolver.

### Honest caveat to state in the report

Playwright's accessibility tree is derived from the browser's DOM. It is a strictly better *abstraction* than CSS selectors and it maps cleanly onto UIA/NSAccessibility for desktop, but on genuinely bad markup it degrades — unnamed generic nodes, table soup. This is exactly why anchor-relative targeting is the primary strategy rather than role+name alone.

---

## 5. Artifact schema

The centerpiece. Pydantic models; JSON Schema export is the agent-facing contract.

### 5.1 Capability

```python
class Capability:
    schema_version: str              # "1.0"
    id: str                          # "member.savings_balance.lookup"
    name: str
    description: str                 # what an LLM reads to decide to call it
    version: str                     # semver, bumped on any step change
    app: AppRef
    surface_kind: Literal["web", "desktop"]
    entry: EntryPoint
    parameters: list[Parameter]
    outputs: list[OutputSpec]
    steps: list[Step]
    outcomes: list[Outcome]
    policy: CapabilityPolicy
    provenance: Provenance
    stability: StabilityRecord | None
```

```python
class AppRef:
    vendor_product: str              # "acme-core-servicing"
    product_version: str | None
    tenant_id: str | None            # None = base capability
    base_capability_id: str | None   # set on tenant specializations
```

```python
class Parameter:
    name: str
    type: Literal["string", "integer", "number", "boolean", "date"]
    required: bool
    sensitive: bool                  # value NEVER persisted to artifact or log
    description: str
    example: str | None              # redacted if sensitive
```

```python
class OutputSpec:
    name: str
    type: str
    source_step_id: str
    description: str
    sensitive: bool
```

### 5.2 Step

```python
class Step:
    id: str
    intent: str                      # human-readable: "search for the member"
    action: Literal["navigate","click","type","select","press_key",
                    "wait_for","extract","assert"]
    target: ControlDescriptor | None
    value: Literal | ParamRef | None
    checkpoint: Condition | None     # asserted after the action
    timeout_ms: int
    risk_class: Literal["safe","risky","irreversible"]
```

### 5.3 ControlDescriptor — the surface abstraction seam

```python
class ControlDescriptor:
    role: str                        # button, textbox, link, cell, combobox
    name: str | None                 # accessible name, if any
    candidates: list[Locator]        # ranked, all verified at record time
```

```python
class Locator:
    strategy: Literal["role_name","anchor_relative","region_ordinal",
                      "text_content","dom_hint"]
    params: dict
    stability_score: float           # 0–1, heuristic — see 5.6
    verified_unique_at_record: bool  # resolved to exactly 1 node during recording
    surface_specific: bool           # dom_hint = True; skipped by desktop resolver
```

Strategy definitions:

| Strategy | Params | Notes |
|---|---|---|
| `role_name` | role, name, match: exact\|contains | Strongest when the app has accessible names |
| `anchor_relative` | anchor_text, anchor_role, relation (same_row \| following \| within_region), target_role, index | **Primary strategy for legacy surfaces.** Survives ID churn and reordering |
| `region_ordinal` | region (heading text / landmark / frame path), role, index | Fallback when nothing is named |
| `text_content` | text, match | Brittle to localisation; low score |
| `dom_hint` | css / xpath | Terminal fallback, web-only, never scored above 0.3 |

### 5.4 Condition — checkpoints and detectors

One type serves both. Checkpoints assert success; detectors identify outcomes.

```python
class Condition:
    kind: Literal["control_present","control_absent","text_present",
                  "url_matches","value_equals"]
    params: dict
    negate: bool = False
```

### 5.5 Outcome — the error taxonomy in the schema

```python
class Outcome:
    name: str                        # "member_not_found"
    kind: Literal["business","recoverable","hard_failure"]
    detect: Condition
    applies_to: list[str] | Literal["any"]   # step ids
    recovery: Recovery | None        # only for kind="recoverable"
    message_template: str
    partial_outputs: list[str]       # outputs still returnable on this outcome
```

```python
class Recovery:
    action: Literal["dismiss_dialog","retry_step","wait_and_retry","reauthenticate"]
    max_attempts: int
    target: ControlDescriptor | None
```

Class definitions, stated in the report because the brief calls conflating them the most common design mistake:

- **business** — a legitimate answer the caller needs. "No such member." Returns `status=business_outcome`, not an exception.
- **recoverable** — a condition the replay handles and continues past. Interstitial modal, transient slow load, one retry on a stale element.
- **hard_failure** — stop, surface a debuggable error. Unknown page state, checkpoint failed with no matching detector, locator chain exhausted.

### 5.6 Locator stability scoring

Heuristic, not measured. Say so in the report.

| Signal | Effect |
|---|---|
| Has a non-empty accessible name | +0.3 |
| Anchored to visible label text | +0.25 |
| Role is specific (button, textbox) vs generic | +0.15 |
| Index-dependent (`index > 0`) | −0.2 |
| Depends on generated ID pattern | −0.4 |
| `surface_specific` | capped at 0.3 |

At record time every candidate is re-resolved against the live page and discarded unless it matches exactly one node. `verified_unique_at_record` is not aspirational.

### 5.7 Provenance and approval

```python
class Provenance:
    discovered_at: datetime
    model: str
    discovery_run_id: str
    state: Literal["draft","approved"]
    approved_by: str | None
    outcomes_reviewed: bool
```

Unattended replay requires `state="approved"` and `outcomes_reviewed=True`.

### 5.8 Tenant overlay

```python
class CapabilityOverlay:
    base_capability_id: str
    base_version: str
    tenant_id: str
    overrides: list[Override]        # {step_id, field_path, value}
    added_outcomes: list[Outcome]
    verified_against: str            # base version last confirmed compatible
```

Resolution: load base → apply overrides by JSON path → validate → replay. Reuse is the default; specialization is additive and auditable. If the base version has moved past `verified_against`, the overlay is flagged `needs_review` and unattended replay is refused.

**Drift detection:** replay records which locator candidate fired per control. When a non-primary candidate fires, or a recoverable outcome triggers on a step that never needed it before, a drift signal is written to the stability record. Crossing a threshold moves the capability back to `draft`.

---

## 6. Discovery run

- Input: goal (natural language) + target entry point + tenant/app ref.
- Loop: `observe()` returns a pruned AX tree plus a screenshot; the model receives the goal, the tree, recent history, and a fixed tool schema of permitted actions; it returns exactly one action.
- Actions are a closed tool schema — the model cannot emit anything the executor doesn't understand.
- Stopping conditions: goal checkpoint satisfied, max steps (25), wall-clock timeout, repeated no-progress (same observation hash 3×), policy block on an irreversible action.
- On success: `LocatorSynthesizer` walks the action trace, generates candidate locators per acted-on control, verifies each, and emits a `Capability` in `draft`.

**Outcome-proposal pass.** Discovery only ever sees the happy path, so the artifact cannot learn failure states from the run that produced it. A bounded second LLM pass reads the recorded flow and proposes exceptional states per step with detectors. Output is advisory: it lands in the artifact only after human approval, which sets `outcomes_reviewed=True`. This gate is doing real work — the model will invent outcomes that do not exist and miss ones that do.

---

## 7. Replay engine

No model in the decision loop.

```
load capability (+ overlay) → validate params against schema
→ acquire session, acquire control lock
→ for each step:
     resolve target via candidate chain (log which candidate fired)
     policy check
     act
     evaluate outcome detectors  → business  → return early with partial outputs
                                 → recoverable → apply recovery, retry (bounded)
                                 → hard_failure → capture evidence, return failure
     assert checkpoint            → failed with no matching detector → hard_failure
→ extract declared outputs → validate against output types → return
```

### Result contract

```python
class ReplayResult:
    status: Literal["success","business_outcome","failure"]
    capability_id: str
    capability_version: str
    run_id: str
    outputs: dict | None
    outcome: OutcomeResult | None    # name, kind, message
    failure: FailureDetail | None    # step_id, expected, observed,
                                     # candidates_tried, evidence_paths
    steps_executed: int
    duration_ms: int
    locator_usage: dict[str, str]    # control → candidate strategy that fired
    drift_signals: list[str]
```

A caller can distinguish "the member does not exist" from "the automation broke" without parsing strings.

---

## 8. Safety model

**Allowlist** (`policy.yaml`, per capability and global):
- permitted domain and route patterns
- permitted action types
- max steps, max wall clock

**Risk classification**, evaluated at the chokepoint:

| Class | Rule | Discovery | Replay |
|---|---|---|---|
| safe | navigate within allowlist, read, extract, type into non-submit fields | allowed | allowed |
| risky | click submit/save/create, form submission | allowed, logged | allowed only if capability `approved` and the step was recorded |
| irreversible | target text or route matches delete / transfer / close / disburse / wire, or step declared irreversible | **blocked → escalate** | **blocked → escalate** |

Irreversible actions are blocked rather than confirmed by the model, because a model that can confirm its own risky action is not a guardrail.

**Data handling:**
- Parameters marked `sensitive` are supplied at invocation and never written to the artifact, logs, or stability records.
- A redaction filter sits on the log writer: pattern-matched account numbers, SSN-shaped strings, card-shaped strings, plus every declared sensitive parameter value, replaced with `[REDACTED:param_name]`.
- **Screenshots are the hard case.** A failure screenshot of a bank screen is full PII and we cannot OCR-redact it in this budget. Default: capture an AX snapshot, not a screenshot. Screenshots require `--allow-screenshots` and are written only to `/evidence/` on a non-production target. This is a policy answer to a technical problem we are not solving, and the report says so.

---

## 9. Escalation and handoff

```python
class InterventionRequest:
    id: str
    run_id: str
    capability_id: str
    step_id: str
    reason: Literal["stuck","risky_action_blocked","unrecoverable","policy_block"]
    goal: str
    state_snapshot: str              # AX tree
    screenshot_ref: str | None
    created_at: datetime
```

```python
class ControlLock:
    session_id: str
    holder: Literal["automation","human","none"]
    acquired_at: datetime
    acquired_by: str
```

Flow:

1. Detector fires (no-progress, unrecoverable outcome, blocked irreversible action).
2. Automation writes the intervention request and **releases the control lock**.
3. The browser window is already headful and visible; the human operates the same session. Nothing is relaunched.
4. A CDP-injected listener records human clicks, inputs (values redacted), and navigations into the run log.
5. The human signals resume (CLI command / file touch / local endpoint). The lock returns to `automation`.
6. Replay re-verifies the current step's checkpoint before continuing — it does not assume the human left the app where it expected.
7. Human actions are appended to the run record and, optionally, offered as a proposed patch to the capability.

**Mocked:** the operator console UI. **Real:** the lock, the request, the same-session control transfer, the action capture, the resume, the post-resume re-verification. The productionised version (containerised browser over noVNC, or a CDP relay with an auth boundary) is described in the report as the seam.

---

## 10. Observability and evidence

`/evidence/` contains, at minimum:

```
/evidence/
  discovery-<run_id>/
    run.jsonl                 structured log: observation hash, model reasoning, action, policy verdict, result
    ax-snapshots/
    screenshots/
    capability.json           the emitted artifact
  replay-success-<run_id>/
    run.jsonl
    result.json
  replay-business-outcome-<run_id>/     # not_found injected
    run.jsonl
    result.json
  replay-failure-<run_id>/              # hard failure injected
    run.jsonl
    result.json
    failure-snapshot.txt
  escalation-<run_id>/
    intervention.json
    human-actions.jsonl
    run.jsonl
  stability/
    replay-x10.json           candidate usage distribution, pass rate
  desktop-ax-proof.txt        AX tree dump of one native app + descriptor resolution
  handoff.mp4                 90s recording (optional but high value)
```

---

## 11. Repo layout

```
/README.md                    setup, keys, one-command demo path
/REPORT.md                    seven required headings
/evidence/
/src/
  surfaces/                   Surface protocol, WebSurface, desktop stub
  session/                    SessionRegistry, ControlLock
  policy/                     PolicyEngine, risk rules, redaction
  discovery/                  agent loop, tool schema, prompts
  recording/                  LocatorSynthesizer, outcome proposal
  schema/                     Pydantic models, JSON Schema export
  replay/                     ReplayEngine, resolvers, outcome evaluation
  escalation/                 intervention, handoff, human capture
  catalog/                    capability catalog + tool-calling surface
/apps/harness/                fault-injection app, tenant-a and tenant-b
/capabilities/                saved artifacts + overlays
/tests/
```

---

## 12. Milestones

| # | Session | Deliverable | Gate |
|---|---|---|---|
| 1 | Green thread | Real LLM run completes a goal on an external surface; policy chokepoint; JSONL log; evidence on disk. `REPORT.md` skeleton started. | **The un-fakeable requirement is retired.** |
| 2 | Harness | Fault-injection app, tenant-a + tenant-b variants | All five failure flags work |
| 3 | Schema | Pydantic models, JSON Schema export, `LocatorSynthesizer` | Candidates verified unique at record |
| 4 | Recording | Discovery emits a real artifact from a real run | Artifact round-trips |
| 5 | Replay | Deterministic executor, resolver chain, checkpoints, outputs | Replay succeeds with no model call |
| 6 | Errors | Outcome-proposal pass + approval gate; failing-replay evidence | `business_outcome` returned as a result, not an exception |
| 7 | Escalation | Lock, intervention, same-session handoff, capture, resume | Human takes over and hands back |
| 8 | Proof | Replay ×10 stability report; desktop AX dump | Determinism measured, not asserted |
| 9 | Tenants + catalog | Overlay resolution across both variants; capability catalog invoked by name with typed args | One artifact runs on both variants |
| 10–11 | Communication | `REPORT.md`, README one-command path, evidence packaging, 90s recording | A stranger can run it |

### Development workflow

Sessions 3–9 parallelise across sub-agents where the work is independent (schema vs harness vs policy vs escalation). Tests are written alongside, and the end-to-end flows are verified in a real browser via Claude in Chrome before evidence is captured.

---

## 13. Feasibility

### Green — high confidence, on budget

- Discovery loop with tool-schema-constrained actions
- Policy chokepoint, allowlist, risk classification
- Fault-injection harness and tenant variant
- Pydantic schema, JSON Schema export, capability catalog
- Deterministic replay engine and result contract
- Three-class outcome taxonomy with declared detectors
- Replay ×10 stability measurement
- Structured logging with pattern-based redaction

### Amber — doable, will cost more than estimated

- **Locator synthesis and ranking.** The verification step is straightforward; the stability scores are judgment, not data. Present them as a heuristic and expect to defend that.
- **Outcome-proposal pass.** Quality is unreliable. The human approval gate is load-bearing, not decorative.
- **Human action capture.** CDP event listeners on a page with framesets require per-frame injection and re-injection on navigation. Budget a full session, not an hour.
- **AX tree quality on hostile markup.** On genuinely bad tables the tree may be thin enough to push you toward `dom_hint` more often than the design wants. If that happens, report the real candidate distribution rather than hiding it — an honest measurement is worth more than a clean claim.
- **One-command demo.** Local app + API key + browser install is three prerequisites. Getting to one command needs deliberate work (a make target, a seeded fixture, a `--offline` replay mode that uses a recorded harness).

### Red — not feasible in this budget; design-only

- **Desktop surface implementation.** Driving a native app via UIA/NSAccessibility is a project in itself. Ship the interface and a twenty-line AX dump proving the descriptor format resolves against a native tree. Anything more is a different take-home.
- **Real co-browsing operator console.** The brief explicitly puts this out of scope. Mock the UI, keep the control-transfer model real.
- **Multi-tenant at genuine scale.** Two variants prove the shape of overlay resolution. They do not prove it survives fifty tenants and three product versions. Say that in the report rather than implying otherwise.
- **Automatic recovery from UI drift.** Detect, signal, and demote to `draft`. Do not attempt to re-derive locators automatically — that is a research problem and the brief does not ask for it.
- **PII redaction from screenshots.** OCR plus region masking is not happening in eleven evenings. The answer is a capture policy (AX snapshots by default), not a redaction pipeline.
- **Authentication, SSO, session establishment.** Assume an authenticated session exists. Out of scope, stated.
- **Vision-only surfaces.** Canvas apps, Citrix-published apps, remote desktop streams. Real in the actual environment, entirely out of scope here — worth one sentence in the report as a known limit of the AX-first approach.

### Known weak points to pre-empt in the report

1. The AX tree is DOM-derived in a browser; the desktop generalisation is argued, not demonstrated.
2. Stability scores are heuristic.
3. The fault-injection harness is self-built, so its failure modes are ones we anticipated. Discovery runs against an external surface to mitigate this.
4. Two tenant variants is a shape proof, not a scale proof.

---

## 14. Open decisions

1. Which external application is the discovery target? Needs a genuine multi-step flow, permissive terms, no real credentials, no real PII.
2. Model API budget ceiling — affects how many discovery runs are affordable and whether the outcome-proposal pass runs per capability.
3. Is the desktop AX dump worth a session, or does the interface stub suffice?
4. Screen recording of the handoff: yes or no?
5. Do we ship the capability catalog as the single stretch goal, or swap it for cross-tenant canonicalisation?