# Understudy — engineering report

An LLM discovers how to accomplish a goal in a UI. The system records that discovery as a typed,
versioned capability artifact and replays it deterministically with no model in the decision loop.

**Citations.** Four evidence files are committed: `desktop-ax-proof.txt`,
`tenant-overlay-proof.txt`, `catalog-agent-demo.txt`, `redaction-proof.txt`. The rest of
`evidence/` is gitignored because run output carries captured page state; those runs are named
by id below and reproduce from the README's commands. 527 tests, 26 files, ruff and mypy strict clean.

## Architecture

Four constraints, each enforced by a test that reads the source rather than by convention
(`tests/test_constraints.py`, 20 tests):

- **C1 — the session outlives the run.** No module under `discovery/`, `replay/` or `recording/`
  may contain `sync_playwright`, `chromium.launch`, `close_all` or `.open(`, and `sync_playwright`
  appears in exactly one file under `src/` (`session/registry.py`). A run that owned its browser
  could not hand the same live window to a human.
- **C2 — one action chokepoint.** An AST walk fails on any `Surface.act()` call inside a function
  that never obtained a `PolicyVerdict`. It caught a real violation: `ReplayEngine._act` acted
  without one. The fix made the verdict a parameter, so the chokepoint is visible in the signature
  (`replay/engine.py:453`).
- **C3 — no surface-specific locator is primary.** No file under `surfaces/` may mention
  `query_selector`, `evaluate`, `css=` or `xpath=`; `Locator._enforce_surface_specificity` derives
  `surface_specific` from the strategy rather than accepting it, and caps `dom_hint` at 0.3.
- **C4 — replay makes zero model calls.** A static import-graph walk finds no `cua.llm` reachable
  from `cua.replay.engine`; a subprocess imports the engine and asserts no `cua.llm` reaches
  `sys.modules`; the constructor is checked to take no LLM collaborator.

Eight further tests check the checkers against violating fixtures, so none can pass vacuously.
That earned its keep: the C2 checker initially counted a `-> PolicyVerdict` return annotation as
evidence of gating, and now scans only the body and parameters (`tests/test_constraints.py:68`).

Observation is the accessibility tree over CDP `Accessibility.getFullAXTree`, pruned per D3 —
mean 46% of nodes kept across the six `ax_pruned` records in
`evidence/discovery-20260910T110803-8bd5f2/run.jsonl`, a run that reached `goal_reached` in six
steps. Chromium's internal roles (`LayoutTableCell`, `LayoutTableRow`) are normalised to ARIA at
the observation boundary; before that fix a replay returned the whole page as the balance and
reported `status: success`.

`recording/assemble.py` closes the loop between the halves: a discovery trace becomes a draft
`Capability` written beside its run, so "record once, replay many times" is a pipeline rather
than a manual step. The draft claims as little as possible - `state=draft`, no outcomes, every
candidate `verified_unique_at_record=false` - because the trace is over by the time it runs and
nothing could be re-resolved.

`catalog/catalog.py` is the agent-facing edge: saved artifacts become tool declarations, and a
tool call becomes a replay. It imports no provider, asserted by a subprocess test.

A third resolver bug surfaced when a recorded draft was checked against its own trace. The
synthesizer resolves a `near` target by walking the accessibility tree; the surface resolved it
by taking the first container *role* that matched, rather than the tightest container that
actually holds a node of the target role. On the results screen the header row holding "Name"
contains `columnheader` nodes and no `cell`, so the surface widened to the outer body row and
answered with a nav link, while the synthesizer answered with the right table cell. A draft
therefore recorded a descriptor for a control the run never touched, silently. Both now apply
the same tightest-valid-container rule, pinned by a test that resolves the same target through
each and fails if they disagree — and that test was checked against the old behaviour to
confirm it catches it.

Two further bugs in the discovery loop surfaced only when real runs were made to reach the goal,
and both had the same shape - something reported success while nothing happened. Model-chosen
targets matched names by substring, so a click on `"Search"` resolved to the `"Member search"`
nav link pointing at the same page: `ok=true`, url unchanged, three turns of confusion. And the
no-progress detector counted an `extract`, which deliberately leaves the page where it is, so
any capability reading two values off one screen was undiscoverable. Both now have regression
tests naming the live run that found them.

## Artifact schema

`schema/models.py`, exported to JSON Schema by `schema/export.py`. A test fails the build on any
property without a description, because an agent decides whether to call a capability from that
schema alone.

- **A control is a ranked chain of locators, never one.** Candidates sort on construction, so
  `candidates[0]` is the primary by definition and a non-primary firing is a defined drift signal.
- **`verified_unique_at_record` is a fact.** `recording/synthesizer.py` re-resolves every candidate
  against the live page and discards any that does not match exactly one node, and that node. Two
  matches is worse than zero, because two matches picks the wrong one silently.
- **A sensitive parameter cannot carry an example**, enforced by the model rather than each caller.
- **`Outcome.kind`** — `business`, `recoverable`, `hard_failure` — is load-bearing. Returning a
  business answer as an exception is the mistake this project exists not to make.

**Weak point: stability scores are heuristic, not measured.** `synthesizer.score()` is the additive
PRD 5.6 rule: a base value, fixed bonuses for an accessible name, a label anchor and a specific
role, penalties for index dependence and generated ids. Nothing in it derives from observed
behaviour. `cua stability` measures what actually fires, and the two disagree — in
`evidence/tenant-overlay-proof.txt` §3 a `role_name` candidate scored 0.85 loses to an
`anchor_relative` candidate scored 0.80 in 10 runs of 10. The score is a prior; the usage histogram
is the evidence. Feeding one back into the other is not built.

## Determinism & error handling

Replay walks the candidate chain in rank order and records which one fired
(`ReplayResult.locator_usage`). Three outcome classes, three behaviours, one run each:

| Class | Evidence | Result |
|---|---|---|
| business | `evidence/replay-business-not-found/` | `business_outcome`, `member_not_found` — returned, not raised |
| recoverable | `evidence/replay-recoverable-modal/` | `success`, `drift_signals: ["read-balance: recovered from 'maintenance_notice' (attempt 1 of 2)"]` |
| hard_failure | `evidence/replay-hard-failure-permission/` | `failure` with candidates tried and an AX snapshot at `failure-read-balance.ax.json` |

A checkpoint that fails with no matching detector is a hard failure, never a silent continue.
Screenshots are off unless `--allow-screenshots`; failure evidence is the AX snapshot. Which of
the three classes a run was is in `result.json`'s `status` rather than the directory name, since
the class is only known once the run ends.

Because replay's decisions come from the artifact and the tree in front of it, the whole path
runs with no browser and no network: `cua replay --offline` plays a recorded tape from
`fixtures/`, and `tests/test_offline.py::test_offline_replay_opens_no_socket` monkeypatches
`socket.connect` and runs that exact path. The tape is strict — it stores the action recorded at
each position and fails if the resolver picks a different control than it did at record time, so
an offline run is a regression test rather than a rehearsal. This is also what lets a reviewer
with no API key exercise the deterministic half.

`cua stability` replays N times and reports the pass rate plus which strategy fired per control.
`deterministic` is stricter than the pass rate: ten passes resolving through different candidates
are ten passes and no determinism. `replay/drift.py` acts on that — a control on a non-primary
candidate in ≥50% of runs, or a step needing a recovery it has no baseline history of needing,
demotes an approved capability to `draft`, which the existing `replayable_unattended` gate refuses.
Withheld below three runs, because one fallback is a flake.

**The most significant known defect.** `member.search`, recorded against member 12345, fails **10
of 10** replays for member 67890 — `read-name` never resolves. Every candidate recorded for that
control is tied to data that happened to be on screen: `role_name` on the member's own name,
anchors on their id and status. The one data-independent candidate is `region_ordinal`, which the
web surface cannot act through. A capability with a `member_id` parameter therefore only works for
the member it was recorded against. Reproduce with `cua stability --capability member.search
--params '{"member_id": "67890"}' -n 10`.

## Heterogeneity & multi-tenant

`apps/harness/` is deliberately legacy-shaped: nested table layout, a per-render generated id on
every element, no test ids, a frameset on the detail screen, submit as an `<a>` with `onclick`, and
query-param flags injecting `not_found`, `permission`, `timeout`, `modal` and `slow`, plus one
screen whose input is genuinely secret so redaction has something to redact. The automation
gets no privileged hook, debug endpoint or application cooperation. A Dolibarr instance under
`apps/dolibarr/` was the third-party target; the harness exists because it fails on demand.

tenant-b is the same app under a different config table (`apps/harness/app.py`, `TENANTS`), not a
fork: three renamed labels, one reordered column, a bumped version. `member.search`, recorded
against tenant-a, replays on tenant-b through an overlay of eight field paths — no re-recording —
10/10 with every control on its recorded primary (`evidence/tenant-overlay-proof.txt` §1–2). An
override matching no field raises rather than applying to nothing. An overlay records
`verified_against`; when the base moves past it the resolved capability is demoted to `draft`
rather than silently reapplied (§4).

**Weak point: two variants prove shape, not scale.** One base and one real overlay show that
resolution works and the staleness rule fires. They say nothing about fifty tenants across three
product versions, where the questions are overlay conflict, inheritance, and who owns
re-verification when a base ships. There is no tenant registry; overlays are files resolved by name.

**Weak point: the desktop story is partly argued.** `evidence/desktop-ax-proof.txt` is a real
result — four of four portable candidates resolve against Windows Calculator's UI Automation tree
through `cua.replay.resolver.matches`, unmodified, with `dom_hint` skipped and varied-argument
checks (`within_region index=3 -> 'Three'`) ruling out coincidence. That demonstrates the
descriptor format is not DOM-shaped. It does not demonstrate a desktop surface: in a browser the AX
tree is computed *from* the DOM, so the roles and names the synthesizer anchors on are downstream
of HTML semantics. A native toolkit produces a differently-shaped tree, and this proof needed a
ten-entry role mapping to align one app's vocabulary. There is no `DesktopSurface`, no acting, no
pruning tuned for a desktop tree. The seam is real; the implementation behind it is not.

## Escalation & handoff

A run that cannot proceed writes an `InterventionRequest` to its evidence directory and *then*
releases the lock. Releasing first would leave a window in which a human could take over with no
record of why.

`evidence/handoff-live-demo/` is a real transfer: stopped at `open-savings`, `reason: stuck`, "The
detail screen is a frameset; no candidate resolved." The run log carries `escalated`,
`intervention_written`, `lock_released`, `human_capture_started`, `human_action`, `resumed`,
`resume_reverified`. One human action was captured:

```json
{"kind": "click", "frame": "accounts", "role": "link", "name": "Open", "value_length": null}
```

The frame name is the point. Capture is injected per frame and re-injected on every navigation; a
listener on the top document alone would record nothing while appearing to work, which is what a
frameset does to naive instrumentation. Typed values never cross the boundary — the page-side
listener reports `value_length` and nothing else, so there is no redaction step to forget. On
resume the run re-verifies the current step's checkpoint, because a human fixing a stuck run may
leave the application somewhere other than where the run expected.

`cua operator` is a mocked console that polls the intervention file and posts a resume signal. It
never touches the lock: a console that could seize control without the run noticing would be a
worse bug than the one it solves. `escalation/operator_app.py` opens with a comment block naming
what is mocked and what production transport would be — a containerised browser over noVNC, or a
CDP relay behind an auth boundary. The lock, transfer, capture and re-verification are real; the UI
is the mock.

## Safety

`PolicyEngine.check()` is the single chokepoint (C2), reading `policy.yaml`, which fails closed: a
missing or malformed policy file is an error, never a permissive default.

- **Irreversible actions block and escalate** in both modes: `delete`, `transfer`, `close`,
  `disburse`, `wire`. Never model-approved — a model that can approve its own risky action is not a
  control. **Risky actions** (`submit`, `save`, `create`) are allowed during discovery, and on
  replay only when a human approved the capability and the step was recorded.
- **Human approval gates unattended execution.** `Provenance.replayable_unattended` requires
  `state="approved"` and `outcomes_reviewed=True`. One property governs three callers — unattended
  replay, overlay staleness, catalog listing — rather than three rules to keep in sync. In
  `evidence/catalog-agent-demo.txt`, withdrawing approval drops the agent's tool list from 2 to 0.
- **Sensitive values never reach disk**, demonstrated rather than asserted. `member.verify`
  declares a sensitive `code`, and the harness screen puts that code in a query string on
  purpose so it lands in a logged field. `evidence/redaction-proof.txt`: the run returns
  "Identity verified", so the value really was typed; the literal appears **0 times** across
  `run.jsonl`, `result.json` and the artifact, with 5 `[REDACTED:code]` markers written
  instead. The code is shaped so the account-/SSN-/card-pattern backstop cannot match it, so
  only the declared value can redact it. The artifact holds a `ParamRef`, never a value, and
  the model nulls the example. **Screenshots default off**: the browser's own url bar held the
  code, and OCR redaction of a servicing screen is not in this budget.

**Weak point: free-tier prompts leave the machine and are retained by the provider.** Every prompt
contains an accessibility tree of whatever is on screen, and on a free tier that content is
retained and may be used to improve the provider's models. The mitigation is not technical: only
synthetic data was ever used. The two harness members are invented, the institution names are
invented, and no real PII, credential or account number has entered a prompt. That is a policy
holding, not an enforced one — nothing in the code stops someone pointing `cua discover` at a
production system. On a paid tier with a zero-retention agreement the constraint disappears.

## Cuts

**Left out, deliberately.**

- **No `DesktopSurface`.** A real surface is observation plumbing, pruning heuristics and an
  action vocabulary for a second toolkit, and it would have cost the replay evidence.
- **No real co-browsing console.** Out of scope per the brief. The transport is the expensive and
  least interesting part.
- **No queue, worker pool, or persistence beyond the filesystem.** Single process, JSON and JSONL
  on disk; scaling infrastructure is an explicit non-goal. **Ollama is interface-only** and never
  produced evidence.
- **The agent demo is one provider,** no streaming, no parallel tool calls, and the tool result
  returns as user content because `Message` carries no `tool_call_id`.

**What I would build next, in order.**

1. **Structural-first locator synthesis.** The 10/10 failure above is the highest-value fix in the
   repo: rank data-independent candidates ahead of ones matching record-time content, and teach the
   web surface to act through `region_ordinal` so the structural candidate is usable rather than
   merely present. Without this, parameterised capabilities are a fiction.
2. **Feed measured usage back into ranking.** `cua stability` already produces the histogram that
   contradicts the heuristic score; persisting it and re-ranking on it closes the loop.
3. **Outcome detection worth trusting.** The model proposed seven detectors for
   `member.savings_balance.read` (`capabilities/member.savings_balance.read.proposals.json`) and
   review accepted **none** — that artifact ships with `outcomes: []`. Permission and modal screens
   therefore reach replay as locator exhaustion (`evidence/live-permission/`, `evidence/live-modal/`):
   truthful, but a worse answer than classification. Where review did accept detectors,
   `member.search` has two and both demonstrably fire, so the gap is proposal quality, not mechanism.
4. **A real `DesktopSurface`,** now that the descriptor side is checked.
5. **Overlay inheritance and conflict rules,** where a third tenant would immediately apply pressure.
