# Understudy

An LLM discovers how to accomplish a goal in a UI. The system records that discovery as a
typed, versioned **capability artifact**. It then replays that artifact deterministically,
with **no model in the decision loop**.

Record once with a model. Replay many times without one.

The economics are the point. On a free tier capped at ~1,500 requests a day, a system that
calls a model on every run does not run. A system that calls one during discovery and none
during replay does. The artifact is not an optimisation — it is the only reason the thing works.

---

## Status

This is an in-progress take-home build. What is real, and what is not, stated plainly:

| Area | State |
|---|---|
| `surfaces/` — AX-tree observation, D3 pruning, role+name+`near` acting | **built, exercised against a live browser** |
| `policy/` — allowlist, risk classification, redaction filter | **built** |
| `llm/` — provider protocol, Gemini, Groq, rate limiter | **built, exercised against a live provider** |
| `discovery/` — the observe/decide/act loop, closed tool schema | **built, completes a real multi-step goal** |
| `evidence/` — JSONL run log, screenshot artifacts | **built** |
| `schema/` — the capability artifact and its JSON Schema export | **built** |
| `recording/synthesizer` — ranked, verified locator candidates | **built, verified against real markup** |
| `replay/resolver` — candidate matching across all five strategies | **built** |
| `session/` — registry holding long-lived browsers | **minimal**: in-process only |
| `replay/engine`, `recording/outcomes`, `escalation/`, `catalog/` | **not yet implemented** — module stubs |

So: discovery works end to end and produces evidence, the artifact it should emit is defined
and enforced, and a control can be described by a ranked chain of verified locators. What is
missing is the executor that walks that chain with no model in the loop. `cua replay`,
`review`, `stability` and `operator` raise `NotImplementedError` today.

205 tests, ruff and mypy strict clean, green on every push.

---

## Quickstart

```bash
uv sync
uv run playwright install chromium
cp .env.example .env          # add GEMINI_API_KEY and/or GROQ_API_KEY (both free, no card)
```

Start the target app, then run a discovery:

```bash
uv run python -m apps.harness 8099

uv run cua discover \
  --goal "Look up member 12345 and read the current balance of their Savings sub-account" \
  --target "http://127.0.0.1:8099/tenant-a/" \
  --provider groq --headless --allow-screenshots
```

That writes `evidence/discovery-<run_id>/run.jsonl` — one structured record per step, carrying
the observation hash, the pruning ratio, the model's stated reasoning, the proposed action, the
policy verdict, the action result and elapsed time.

A clean run looks like this:

```
step 1: type    textbox "Member id"
step 2: click   button  "Search"
step 3: click   link    "Open"
step 4: click   link    "Open"  near="Savings"
step 5: extract cell            near="Current balance" nth=1  -> "4,182.55"
step 6: finish  outputs={"Current balance": "4,182.55"}
```

Six steps, six model calls, no wrong turns. That is the real trace from
`evidence/discovery-20260910T110803-8bd5f2/`, reproduced verbatim; the harness has since
renamed two of those labels (`Member ID`, `Savings Balance`), which is exactly the drift a
tenant overlay has to absorb.

---

## The automation gets no special treatment

Stated plainly because it is the first thing worth checking:

- **No privileged hooks.** No debug endpoint, no injected helper, no application cooperation
  of any kind.
- **No test ids.** The harness emits a freshly generated element id on every element on every
  render, and there is not a `data-testid` anywhere in it. Nothing can be located by id twice.
- **No special endpoints.** The automation drives the same routes and the same markup a human
  operator sees, over the same HTTP.
- **No DOM selectors.** Observation is the accessibility tree over CDP. Acting is role plus
  accessible name, optionally scoped by a nearby anchor. There is a test that fails if any file
  under `surfaces/` so much as mentions `query_selector`, `evaluate`, `css=` or `xpath=`.

The target app is deliberately legacy-shaped: nested table layout, generated ids, a frameset
screen, and query-param flags that inject `not_found`, `permission`, `timeout`, `modal` and
`slow` failures on demand. A second tenant variant renames two labels and reorders a column.

---

## The artifact

A capability is a typed, versioned recording: an entry point, declared parameters and
outputs, ordered steps, and the outcomes it knows how to recognise. Its JSON Schema is
exported to `capabilities/schema/` and is the contract an agent reads to decide whether and
how to call it, so every property in it carries a description and a test fails the build on
any that does not.

Four things the type system enforces rather than the documentation asking for:

- **A control is a ranked chain of locators, never one locator.** Candidates are sorted on
  construction, so `candidates[0]` is the primary by definition and a non-primary firing
  during replay is a well-defined drift signal.
- **`surface_specific` is derived, not accepted.** A `dom_hint` is forced surface-specific and
  capped at 0.3, so a css selector cannot present itself as portable or outrank an anchor.
- **`verified_unique_at_record` is a fact.** Every candidate is re-resolved against the live
  page and discarded unless it matches exactly one node — and that node. Two matches is worse
  than none, because two matches picks the wrong one silently.
- **A sensitive parameter cannot carry an example.** An example of a real account number is a
  real account number, so the model nulls it rather than trusting each call site.

Locator candidates span all five strategies, and the three `anchor_relative` relations
(`same_row`, `following`, `within_region`) resolve **structurally against the accessibility
tree** rather than the DOM. That is what makes the params portable: the same locator resolves
against a desktop AX tree with the same code.

---

## Four architectural constraints

Violating any of these is a bug, not a style choice. Two are enforced by tests that read the
source, not by convention.

- **C1 — the session outlives the run.** A human takes control of the *same live session*, so
  runs attach to a browser held by `SessionRegistry`; they never launch or close their own.
- **C2 — one action chokepoint.** Every action passes `PolicyEngine.check()` before touching a
  surface. *Enforced:* an AST test fails on any `Surface.act()` call whose function never
  obtained a `PolicyVerdict`.
- **C3 — no surface-specific locator is ever a primary strategy.** Role, name and containment
  lead; a DOM hint is a terminal fallback. *Enforced twice:* the selector-API test above, and
  the schema itself, which derives `surface_specific` from the strategy and caps its score.
- **C4 — replay makes zero model calls.** `ReplayEngine` will take no `LLMClient` in its
  dependency graph. To be enforced structurally when replay lands.

---

## Safety

- **Irreversible actions block and escalate**, in both discovery and replay. Delete, transfer,
  close, disburse and wire are never model-approved — a model that can approve its own risky
  action is not a control.
- **Risky actions** (submit, save, create) are allowed during discovery and, on replay, only
  when a human approved the capability *and* the step was actually recorded.
- **Sensitive values never reach disk.** Parameters marked sensitive are supplied per
  invocation; a redaction filter on the log writer is the backstop, catching declared values
  plus account-, SSN- and card-shaped strings.
- **Screenshots default to off.** With `--allow-screenshots` they are written as files in the
  evidence directory and never inlined into a log record. Whether they also reach the model is
  a separate decision, made by the provider's capabilities rather than the flag.
- **Synthetic data only.** Free-tier prompts leave the machine and are retained by the
  provider. No real people, institutions or account numbers appear anywhere, by policy rather
  than by luck.

`policy.yaml` holds the allowlist, the run limits and the risk keywords. It fails closed: a
missing or malformed policy file is an error, never a permissive default.

---

## Free-tier notes

- **Gemini** (Flash) runs the vision-capable discovery loop. Keep billing **disabled** on the
  project — enabling it deletes the free tier, and every call bills from the first token.
- **Groq** runs text-only passes. Its binding constraint is tokens, not requests: measured at
  8000 TPM against 1000 requests/minute, so an accessibility tree exhausts the token budget
  long before the request budget.
- The rate limiter paces every call through a token bucket and backs off on 429. It
  distinguishes the two flavours: per-minute exhaustion is retried, **daily** exhaustion raises
  immediately rather than retrying for hours. The fixed 1/2/4/8 schedule is a floor — the
  provider's own `retry-after` hint wins when it is longer, because a fixed 15 seconds can
  never outlast a 60-second token window.

---

## Layout

```
src/cua/
  surfaces/   Surface protocol, WebSurface (AX via CDP), pruning, desktop stub
  session/    SessionRegistry, ControlLock
  policy/     PolicyEngine chokepoint, risk rules, redaction filter
  llm/        LLMClient protocol, Gemini, Groq, Ollama, rate limiter
  discovery/  the loop, the closed tool schema, prompts
  recording/  LocatorSynthesizer; outcome proposal          (outcomes: stub)
  schema/     Pydantic capability models, JSON Schema export
  replay/     candidate resolver; executor, conditions       (engine: stub)
  escalation/ intervention, handoff, operator console        (stub)
  catalog/    capability catalog                             (stub)
  evidence/   JSONL logger with redaction
apps/harness/ fault-injection target app, tenant-a and tenant-b
capabilities/ saved artifacts, overlays, and the exported JSON Schema
evidence/     run output (gitignored)
```

Design documents: `prd.md` (scope, schema, milestones) and `tech.md` (architecture, stack,
free-tier strategy). `CLAUDE.md` is the standing context for work on this repo.

---

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Full type annotations, Pydantic for everything persisted, no bare excepts, structured JSONL
logging only, and no `print` in library code. An AST test also fails the build if a structured
log field shadows a `LogRecord` attribute — that one is invisible until a handler puts the
logger at INFO, and then it is fatal.

CI runs lint, types and tests on every push. It installs Chromium, because locator synthesis
is verified against a real accessibility tree rather than a mock of one. It makes no model
calls and needs no secrets.
