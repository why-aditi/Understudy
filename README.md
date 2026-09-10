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
| `surfaces/` — AX-tree observation, D3 pruning, role+name+`near` acting | **built**, exercised against a live browser |
| `policy/` — allowlist, risk classification, redaction filter | **built** |
| `llm/` — provider protocol, Gemini, Groq, rate limiter | **built**, exercised against a live provider |
| `discovery/` — the observe/decide/act loop, closed tool schema | **built**, completes a real multi-step goal |
| `schema/` — the capability artifact and its JSON Schema export | **built** |
| `recording/` — locator synthesis, outcome proposal, human approval gate | **built** |
| `replay/` — candidate resolver, condition evaluator, deterministic engine | **built**, replays a real capability with no model |
| `escalation/` — intervention, lock transfer, human capture, resume, console | **built**, exercised through a real frameset |
| `session/` — registry owning browsers, `ControlLock` | **built**, in-process only |
| `evidence/` — JSONL run log, screenshots, `result.json` | **built** |
| `catalog/` — capabilities as callable typed tools | **not yet implemented** |
| overlay resolution — one artifact across two tenants | **not yet implemented** |
| `surfaces/desktop.py`, `llm/ollama.py` | **interface only**, deliberately |
| the descriptor format off the web | **proven, not built** — resolved against a live Windows AX tree, no `DesktopSurface` |

So: a capability can be recorded from a live application, reviewed by a human, replayed
deterministically with no model in the loop, and handed to a human and back when it gets
stuck. What is missing is the catalog that exposes capabilities as agent-callable tools, and
the overlay that lets one artifact serve two tenants.

Every CLI subcommand is implemented.

392 tests, ruff and mypy strict clean, green on every push.

---

## Quickstart

```bash
uv sync
uv run playwright install chromium
cp .env.example .env          # add GEMINI_API_KEY and/or GROQ_API_KEY (both free, no card)
```

Start the target app, then run the loop:

```bash
uv run python -m apps.harness 8099

# 1. discover: an LLM drives the app and the run is written to evidence/
uv run cua discover \
  --goal "Look up member 12345 and read their Savings balance" \
  --target "http://127.0.0.1:8099/tenant-a/" \
  --provider groq --headless --allow-screenshots

# 2. review: a model proposes outcomes; a human accepts, edits or rejects each one
uv run cua review --capability member.search

# 3. replay: deterministic, no model in the decision loop
uv run cua replay --capability member.search --params '{"member_id": "12345"}'

# 4. operator: the console that shows a stalled run and hands control back
uv run cua operator
```

A discovery writes `evidence/discovery-<run_id>/run.jsonl` — one structured record per step,
carrying the observation hash, the pruning ratio, the model's stated reasoning, the proposed
action, the policy verdict, the action result and elapsed time. A replay writes
`evidence/replay-<run_id>/` with both `run.jsonl` and `result.json`: how it went, and what
the caller was told.

`evidence/` is gitignored, because run output carries captured page state. So the runs quoted
throughout this README are **reproducible from the commands above rather than checked in** —
the run ids name real directories on the machine they were produced on, not paths in this
repo. The single exception is `evidence/desktop-ax-proof.txt`, which contains no application
state and is committed.

A clean discovery looks like this:

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

And a replay returns a typed result rather than a string to parse:

```
$ cua replay --capability member.search --params '{"member_id": "12345"}'
  status: success            outputs: {"member_name": "Wilhelmina Okonkwo-Bright"}

$ cua replay --capability member.search --params '{"member_id": "99999"}'
  status: business_outcome   outcome: member_not_found
```

The second is not an error. "No such member" is an answer the caller asked for, and telling
it apart from "the automation broke" without parsing a message is the point of the artifact.

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
tree** rather than the DOM. That is what makes the params portable — and it is checked rather
than asserted, below.

---

## The descriptor format, off the web

The claim C3 rests on is that a recorded control descriptor is not a web artifact: strip the
one strategy flagged `surface_specific` and the rest should resolve against any accessibility
tree. That is easy to assert and cheap to check, so it is checked.

`scripts/desktop_ax_proof.py` dumps the UI Automation tree of **Windows Calculator**, maps UIA
control types onto the role vocabulary the web surface emits, and resolves candidates against
it through `cua.replay.resolver.matches` — unmodified, the same function replay itself calls.

```
application : 'Calculator' (WindowControl)
resolver    : cua.replay.resolver.matches, unmodified

  role_name                        resolved                    'Seven'
  anchor_relative / within_region  resolved                    'Seven'
  anchor_relative / following      resolved                    'Five'
  text_content                     resolved                    'Equals'
  dom_hint                         SKIPPED (surface_specific)  cannot be expressed here

The relations are structural, not coincidence - vary the argument:

  within_region index=3            -> 'Three'
  following 'Seven'                -> 'Eight'
  following 'Memory recall'        -> 'Memory add'
```

Four of four portable candidates resolve. `dom_hint` being skipped is the *positive* result:
it is the one strategy the schema derives as surface-specific, and this is the first surface
with no DOM for it to mean anything against.

The varied-argument block matters more than the count. Resolving `index=7` to `'Seven'` on a
calculator is exactly the kind of result that could be coincidence; changing the argument and
getting the correspondingly different control is what rules that out.

Making this work required a ten-entry dict mapping UIA control types to ARIA roles, and no
change to the resolver. **There is no `DesktopSurface` and this does not build one** — it is
a proof that the seam is real, not an implementation behind it. Full output in
`evidence/desktop-ax-proof.txt`; re-run it with
`uv run python scripts/desktop_ax_proof.py` on Windows.

---

## Four architectural constraints

Violating any of these is a bug, not a style choice. **All four are enforced by tests that
read the source**, not by convention.

- **C1 — the session outlives the run.** `SessionRegistry.open()` creates a session and
  belongs to whoever owns the registry; `attach()` only ever returns one that already
  exists, so a run cannot create a browser by asking for it, and never closes one.
  *Enforced:* no module under `discovery/`, `replay/` or `recording/` may launch or close a
  browser, and `sync_playwright` appears in exactly one file in the tree.
- **C2 — one action chokepoint.** Every action passes `PolicyEngine.check()` before touching
  a surface. *Enforced:* an AST test fails on any `Surface.act()` call whose function never
  obtained a `PolicyVerdict`.
- **C3 — no surface-specific locator is ever a primary strategy.** Role, name and containment
  lead; a DOM hint is a terminal fallback. *Enforced twice:* no file under `surfaces/` may
  mention `query_selector`, `evaluate`, `css=` or `xpath=`, and the schema itself derives
  `surface_specific` from the strategy and caps its score at 0.3. *Demonstrated:* the
  remaining strategies resolve against a live Windows accessibility tree through the
  unmodified resolver — see [above](#the-descriptor-format-off-the-web).
- **C4 — replay makes zero model calls.** *Enforced three ways:* a static import-graph walk
  finds no `cua.llm` reachable from `cua.replay.engine`; a subprocess imports the engine and
  asserts no `cua.llm` module ends up in `sys.modules`; and the constructor is checked to
  take no LLM collaborator.

Each checker is itself tested against a deliberately violating fixture, so none of them can
pass vacuously.

On top of the four, a fifth guard: an AST test fails the build if a structured log field
shadows a `LogRecord` attribute. That one is invisible until a handler puts the logger at
INFO, and then it is fatal — it was found the hard way.

---

## Measuring determinism rather than claiming it

"Replay is deterministic" is a claim; `cua stability` turns it into a number. It costs no
model calls, which is the whole point — running a capability ten times is free.

```
$ cua stability --capability member.search --params '{"member_id": "12345"}' -n 10

  runs         10
  pass rate    10/10 (100%)
  deterministic True
  duration     median 134 ms

  per control, which candidate actually fired:
    enter-id             primary=role_name        role_name 10x
    read-name            primary=role_name        role_name 10x
    submit-search        primary=role_name        role_name 10x
```

The pass rate is the least interesting number here. **Which candidate fired** is the one that
matters: a capability can pass ten out of ten while quietly resolving through its third
candidate every run, which means the recorded primary is already dead and only the chain is
holding it up. That is a finding about our own ranking heuristic, so `non_primary_rate` is
computed per control and anything notable is written into a `findings` list in plain
sentences. `deterministic` is deliberately stricter than the pass rate: ten passes that each
resolve through a different candidate are ten passes and no determinism at all.

Written to `evidence/stability/replay-x10.json`.

**A finding this command produced on its first real use.** Replaying the same capability for a
*different* member fails 10/10:

```
    read-name            primary=role_name        never resolved

  findings:
    - read-name: never resolved in any run - the whole candidate chain was exhausted
      every time, so nothing recorded for this control works against the screen as it is now.
```

Every candidate the synthesizer recorded for that control is tied to the data that happened
to be on screen at record time — `role_name` on *the member's own name*, anchors on their id
and status. The one structural, data-independent candidate is `region_ordinal`, and the web
surface cannot act through it. So a capability with a `member_id` parameter only works for the
member it was recorded against. That is measured, not suspected, and it is the next thing to
fix in the synthesizer.

---

## Handing control to a human

A run that cannot proceed writes an `InterventionRequest` to its evidence directory —
reason, capability, step, url, and the accessibility snapshot of the screen it stopped on —
and *then* releases the lock. That order matters: releasing first would leave a window a
human could take over with no record of why.

While the human holds the lock the session records what they do, injected **per frame and
re-applied on every navigation**. The harness's detail screen is a frameset, and a listener
installed only on the top document would record nothing at all while appearing to work.
Typed values never cross the boundary: the page-side listener reports the *length* of an
input and nothing else, so there is no redaction step to forget.

`cua operator` serves a console showing the pending request and a Resume button. The console
is the mocked part and says so at the top of its own source. It never touches the lock — it
writes a signal, and the run decides when to take control back, then **re-verifies the
step's checkpoint** before continuing, because a human fixing a stuck run may leave the
application two screens from where the run expected it.

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
  session/    SessionRegistry (owns browsers), ControlLock
  policy/     PolicyEngine chokepoint, risk rules, redaction filter
  llm/        LLMClient protocol, Gemini, Groq, Ollama, rate limiter
  discovery/  the loop, the closed tool schema, prompts
  recording/  LocatorSynthesizer, outcome proposal, approval gate
  schema/     Pydantic capability models, JSON Schema export
  replay/     candidate resolver, condition evaluator, deterministic engine
  escalation/ intervention record, handoff, capture, mocked operator console
  catalog/    capability catalog                             (stub)
  evidence/   JSONL logger with redaction
apps/harness/ fault-injection target app, tenant-a and tenant-b
capabilities/ saved artifacts, overlays, and the exported JSON Schema
scripts/      one-shot proofs whose output is the deliverable, not library code
evidence/     run output (gitignored, except the checked-in desktop proof)
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
