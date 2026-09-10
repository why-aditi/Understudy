# Understudy

An LLM discovers how to accomplish a goal in a UI. The system records that discovery as a
typed, versioned **capability artifact**. It then replays that artifact deterministically,
with **no model in the decision loop**.

Record once with a model. Replay many times without one.

The economics are the point. On a free tier capped at ~1,500 requests a day, a system that
calls a model on every run does not run. A system that calls one during discovery and none
during replay does. The artifact is not an optimisation — it is the only reason the thing works.

---

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/). Nothing else, and no API key to start.

```bash
git clone https://github.com/why-aditi/Understudy && cd Understudy
uv sync
```

## Demo path

Three steps, each needing strictly more setup than the last. **Step 1 needs no key, no browser
and no network** — it is there so the central claim can be checked in under a minute.

### 1. Deterministic replay, offline (no key, no browser, no network)

```bash
uv run cua replay --capability member.search --params '{"member_id": "12345"}' --offline
```

```json
{
  "status": "success",
  "outputs": {"member_name": "Wilhelmina Okonkwo-Bright"},
  "steps_executed": 3,
  "locator_usage": {"enter-id": "role_name", "submit-search": "role_name", "read-name": "role_name"},
  "drift_signals": []
}
```

That replayed a capability recorded from a real browser against a recorded fixture
(`fixtures/member.search.fixture.json`). Reading that file is the only I/O it performs — a test
monkeypatches `socket.connect` and runs this exact path to prove it
(`tests/test_offline.py::test_offline_replay_opens_no_socket`).

The fixture is a **strict** tape. It stores the action recorded at each position and refuses a
run that diverges: if the resolver picks a different control than it did at record time, you get
a mismatch naming both actions rather than a green tick. A lenient fixture would pass every time
and prove nothing.

```bash
uv run cua replay --capability member.search --params '{"member_id": "67890"}' --offline
# error: this fixture was recorded with params {"member_id": "12345"}; you passed ...
```

Also keyless — the same capabilities as the typed tools an agent would be handed:

```bash
uv run cua catalog
```

### 2. Against the live app (needs Chromium, still no key)

```bash
uv run playwright install chromium
```

In a second terminal, start the target app and leave it running:

```bash
uv run python -m apps.harness 8099
```

Then:

```bash
# the same capability, now driving a real browser
uv run cua replay --capability member.search --params '{"member_id": "12345"}'

# the same artifact on a second tenant, through an overlay, with no re-recording
uv run cua replay --capability member.search --params '{"member_id": "12345"}' --tenant tenant-b

# replay it ten times and report which locator candidate actually fired
uv run cua stability --capability member.search --params '{"member_id": "12345"}' -n 10

# record your own offline fixture from a live run
uv run cua replay --capability member.search --params '{"member_id": "12345"}' --record-fixture

# a capability with a *sensitive* parameter: the run succeeds, the value never lands
uv run cua replay --capability member.verify --params '{"code": "QX7-4412"}'
grep -r "QX7-4412" evidence/replay-*/     # nothing; the log holds [REDACTED:code]
```

That last pair is worth running together. `"Identity verified"` comes back only for the
correct code, so the value really was typed — and the harness puts it in a query string on
purpose, so it lands in a logged field and the filter has to catch it there rather than the
value simply never being written.

### 3. With a model (needs a free key)

```bash
cp .env.example .env      # then paste a key into it
```

**Discover, then replay what you just discovered.** This is the whole thread in two commands:

```bash
# 1. an LLM drives the app. The run, the trees it reasoned over, and the draft artifact it
#    produced all land in evidence/discovery-<run_id>/
uv run cua discover --goal "Search for member 12345 and read the member name shown in the results table" --target "http://127.0.0.1:8099/tenant-a/" --provider groq --vendor-product meridian-core --headless

# 2. replay that artifact deterministically, with no model in the decision loop
uv run cua replay --capability "$(ls -d evidence/discovery-*/capability.json | tail -1)" --attended
```

`--capability` takes an id or a path, so the artifact replays where discovery left it — no
copy-and-rename step in the middle of the one flow that matters. `--attended` is required
because the emitted artifact is a **draft**: `state=draft`, no outcomes, every candidate
`verified_unique_at_record=false`. The model chose those controls; until a human agrees, it
cannot replay unattended. That gate is the same one that governs overlay staleness and what
the agent catalog will list.

A committed run of exactly this is in
[`evidence/discovery-20260910T211157-fb8cb8/`](evidence/discovery-20260910T211157-fb8cb8),
if you would rather read one than run one.

Also with a key:

```bash
# an LLM picks a capability by name and calls it with typed args
uv run python scripts/agent_demo.py
```

## Keys

Both providers are free and **neither asks for a card**. Either one alone is enough, and only
step 3 needs one at all.

| Variable | Where to get it | Used for | Limit that binds |
|---|---|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | the vision-capable discovery loop | ~15 requests/minute |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | text-only passes and the agent demo | 8000 tokens/minute |

Copy `.env.example` to `.env` and paste in whichever you have. `.env` is gitignored, and CI makes
no model calls and needs no secrets. **Keep billing disabled on the Gemini project** — enabling it
deletes the free tier, and every call then bills from the first token.

Rate limits, not cost, are the binding constraint. Every call goes through a token-bucket limiter
that backs off on 429 and tells the two flavours apart: per-minute exhaustion is retried, daily
exhaustion raises immediately rather than retrying for hours.

## The automation receives no privileged access

Stated plainly because it is the first thing worth checking, and it holds on every surface:

- **No privileged hooks.** No debug endpoint, no injected helper, no application cooperation of
  any kind. The target app does not know it is being automated.
- **No test IDs, injected or otherwise.** The harness emits a freshly generated element id on
  every element on every render, and there is no `data-testid` anywhere in it. Nothing can be
  located by id twice, by design.
- **No special endpoints.** The automation drives the same routes and the same markup a human
  operator sees, over the same HTTP — no query parameter or header that changes behaviour in its
  favour. The `?fail=` flags inject failures; they never make anything easier.
- **No DOM selectors.** Observation is the accessibility tree over CDP. Acting is role plus
  accessible name, optionally scoped by a nearby anchor. A test fails if any file under
  `surfaces/` so much as mentions `query_selector`, `evaluate`, `css=` or `xpath=`.

The same holds for the Dolibarr instance under `apps/dolibarr/`: stock, unmodified, and seeded
through its own web UI.

---

## Status

What is real, and what is not, stated plainly:

| Area | State |
|---|---|
| `surfaces/` — AX-tree observation, D3 pruning, role+name+`near` acting | **built**, exercised against a live browser |
| offline replay — recorded fixtures, no browser, no network | **built**, a socket-blocked test proves it |
| `policy/` — allowlist, risk classification, redaction filter | **built**, a run shows a sensitive value not reaching disk |
| `llm/` — provider protocol, Gemini, Groq, rate limiter | **built**, exercised against a live provider |
| `discovery/` — the observe/decide/act loop, closed tool schema | **built**, completes a real multi-step goal |
| `schema/` — the capability artifact and its JSON Schema export | **built** |
| `recording/` — locator synthesis, outcome proposal, human approval gate | **built** |
| `replay/` — candidate resolver, condition evaluator, deterministic engine | **built**, replays a real capability with no model |
| `escalation/` — intervention, lock transfer, human capture, resume, console | **built**, exercised through a real frameset |
| `session/` — registry owning browsers, `ControlLock` | **built**, in-process only |
| `catalog/` — capabilities as callable typed tools | **built**, an LLM calls one end to end |
| overlay resolution — one artifact across two tenants | **built**, replays on tenant-b with no re-recording |
| drift detection — non-primary hits and new recoveries demote to draft | **built**, measured on a real fallback |
| `surfaces/desktop.py`, `llm/ollama.py` | **interface only**, deliberately |
| the descriptor format off the web | **proven, not built** — resolved against a live Windows AX tree, no `DesktopSurface` |

`REPORT.md` is the engineering report: what was built, where it is weak, and what was cut.

546 tests, ruff and mypy strict clean, green on every push.

A discovery writes `evidence/discovery-<run_id>/`:

```
run.jsonl        one record per step: observation hash, pruning ratio, the model's stated
                 reasoning, the proposed action, the policy verdict, the result, elapsed time
ax-snapshots/    the pruned tree the model actually reasoned over, one per step
screenshots/     only with --allow-screenshots
capability.json  the draft artifact the run produced
```

`capability.json` is the join between the two halves: `recording/assemble.py` turns the trace
into a `Capability` a human can review and replay. It is emitted as a **draft** — `state=draft`,
no outcomes, every candidate `verified_unique_at_record=false` — because the model chose those
controls and nobody has agreed they are the right ones.

The draft only records what the run actually did. An `extract` carries ground truth, so its
descriptor is cross-checked against the text the surface returned and dropped if they disagree:
a descriptor naming a control the run never touched is worse than no step at all. Consecutive
reads of the same control collapse to one, because a read has no side effect and repeating it
is the model repeating itself.

A replay writes `evidence/replay-<run_id>/` with `run.jsonl` and `result.json`. Which of the
three outcome classes a run was is in `result.json`'s `status`, not in the directory name: the
class is only known once the run ends.

A named set of runs is committed: one discovery run with its trees, screenshots and emitted
artifact, one replay per outcome class, and four standalone proofs. See
[`evidence/README.md`](evidence/README.md) for what each one shows. The rest of `evidence/` is
gitignored — in production run output would carry captured page state — so the other run ids
quoted below name directories on the machine that produced them and are reproducible from the
commands above rather than checked in. What *is* published is listed by name in `.gitignore`,
so it is a decision rather than an accident.

A clean discovery looks like this:

```
step 1: type    textbox "Member ID" = "12345"
step 2: click   link    "Search"
step 3: extract cell    near="Name" nth=1 -> "Wilhelmina Okonkwo-Bright"
step 4: finish  outputs={"member_name": "Wilhelmina Okonkwo-Bright"}
```

Four steps, four model calls, no wrong turns — the real trace from the committed run
[`evidence/discovery-20260910T211157-fb8cb8/`](evidence/discovery-20260910T211157-fb8cb8),
reproduced verbatim. Step 3 is the interesting one: `near="Name"` scopes to the tightest
container that actually holds a cell, which is the results table rather than the header row the
anchor sits in. An earlier build resolved that to a nav link instead, and the model burned two
turns recovering — the same target now lands first time.

And a replay returns a typed result rather than a string to parse:

```
$ cua replay --capability member.search --params '{"member_id": "12345"}'
  status: success            outputs: {"member_name": "Wilhelmina Okonkwo-Bright"}

$ cua replay --capability member.search --params '{"member_id": "99999"}'
  status: business_outcome   outcome: member_not_found
```

The second is not an error. "No such member" is an answer the caller asked for, and telling it
apart from "the automation broke" without parsing a message is the point of the artifact.

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
  browser, and `sync_playwright` appears in exactly one file under `src/`.
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
    read-name            primary=anchor_relative  anchor_relative 10x
    submit-search        primary=role_name        role_name 10x
```

The pass rate is the least interesting number here. **Which candidate fired** is the one that
matters: a capability can pass ten out of ten while quietly resolving through its third
candidate every run, which means the recorded primary is already dead and only the chain is
holding it up. That is a finding about our own ranking heuristic, so `non_primary_rate` is
computed per control and anything notable is written into a `findings` list in plain
sentences. `deterministic` is deliberately stricter than the pass rate: ten passes that each
resolve through a different candidate are ten passes and no determinism at all.

Written to `evidence/stability/replay-x10.json`, alongside the drift verdict in
`drift-x10.json`.

**A finding this command produced on its first real use, and what it cost to fix.** Replaying
the same capability for a *different* member used to fail 10/10:

```
    read-name            primary=role_name        never resolved

  findings:
    - read-name: never resolved in any run - the whole candidate chain was exhausted
      every time, so nothing recorded for this control works against the screen as it is now.
```

Every candidate recorded for that control was tied to data that happened to be on screen at
record time — `role_name` on *the member's own name*, anchors on their id and status. A
capability with a `member_id` parameter worked for exactly the member it was recorded against.

The candidate that should have worked was already in the chain and already actionable: *the
cell in the same row as the member id*. It failed only because the anchor was frozen as the
literal `"12345"` instead of following the parameter. `Locator.binds` fixes that — the anchor
is substituted per invocation — and synthesis now ranks a bound candidate above one identified
by record-time content, which for an `extract` is circular anyway: the identifying text is the
value you are trying to read.

```
$ cua stability --capability member.search --params '{"member_id": "67890"}' -n 10
  pass rate    10/10 (100%)          # was 0/10
  read-name    primary=anchor_relative   anchor_relative 10x
  no drift: 10 run(s), every control resolved through its primary
```

The fix is bounded, and the README should say where it stops: `region_ordinal` and the
`following`/`within_region` relations are still unreachable from the web surface, so a control
with **no** usable anchor still has no data-independent candidate. Binding raises the floor
rather than removing it. `REPORT.md` carries the rest, including the second bug this surfaced —
a skipped candidate being miscounted as drift, which would have demoted a healthy capability on
every run.

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

`uv run python scripts/handoff_demo.py` runs the whole transfer against the harness and writes
[`evidence/handoff-live-demo/`](evidence/handoff-live-demo). `cua operator` serves a console
showing the pending request and a Resume button. The console
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
- **Sensitive values never reach disk**, and there is a run that shows it.
  `cua replay --capability member.verify --params '{"code": "QX7-4412"}'` succeeds — so the
  value really was typed — while the literal appears zero times in `run.jsonl`, `result.json`
  or the artifact, replaced by `[REDACTED:code]`. The harness puts the code in a query string
  deliberately, so it lands in a logged field and the filter has to catch it there. That code
  is invented harness fixture data, like every other value in this repo — it is printed here
  so the command is runnable, and it is a secret only in the sense that the system treats it
  as one.
  `evidence/redaction-proof.txt`.
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

The `Keys` section above covers where to get them. Two details that shaped the design:

- **Groq's binding constraint is tokens, not requests** — measured at 8000 TPM against 1000
  requests/minute, so an accessibility tree exhausts the token budget long before the request
  budget. That is why observation is pruned rather than sent whole.
- **The limiter's 1/2/4/8 backoff is a floor**, not a schedule. The provider's own `retry-after`
  hint wins when it is longer, because a fixed 15 seconds can never outlast a 60-second token
  window.

---

## Layout

```
src/cua/
  surfaces/   Surface protocol, WebSurface (AX via CDP), pruning, offline fixture
              surface and recorder, desktop stub
  session/    SessionRegistry (owns browsers), ControlLock
  policy/     PolicyEngine chokepoint, risk rules, redaction filter
  llm/        LLMClient protocol, Gemini, Groq, Ollama, rate limiter
  discovery/  the loop, the closed tool schema, prompts
  recording/  LocatorSynthesizer, trace-to-draft assembly, outcome proposal, approval gate
  schema/     Pydantic capability models, JSON Schema export
  replay/     candidate resolver, condition evaluator, deterministic engine,
              tenant overlay resolution, stability measurement, drift verdicts
  escalation/ intervention record, handoff, capture, mocked operator console
  catalog/    discovery, tool-schema generation, typed invocation by name
  evidence/   JSONL logger with redaction
apps/harness/ fault-injection target app, tenant-a and tenant-b, and one screen whose
              input is genuinely secret
capabilities/ saved artifacts, tenant overlays, and the exported JSON Schema
fixtures/     recorded tapes for offline replay, committed so a reviewer needs no key
scripts/      one-shot proofs whose output is the deliverable, not library code:
              the desktop AX proof, the agent demo, the escalation handoff
evidence/     run output (gitignored, except the checked-in proofs)
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

The offline replay path is covered end to end, including a test that blocks `socket.connect`
and runs it anyway. CI runs lint, types and tests on every push, over `src/`, `tests/`,
`apps/` and `scripts/`.
It installs Chromium, because locator synthesis is verified against a real accessibility tree
rather than a mock of one. It makes no model calls and needs no secrets — which is also why
the two scripts under `scripts/` are checked but never executed there: one needs a Windows
accessibility API, the other needs an API key.
