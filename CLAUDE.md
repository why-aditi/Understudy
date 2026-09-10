# CLAUDE.md — Understudy

## What this project is

A system that uses an LLM to **discover** how to accomplish a goal in a UI, **records** that
discovery as a typed, versioned, reusable **capability artifact**, and **replays** the artifact
deterministically with **no model in the decision loop**.

Record once with a model. Replay many times without one. Everything else in this repo exists to
make that sentence true and provable.

It is a hiring artifact (interface.ai take-home), graded by humans reading `REPORT.md` and
spot-checking that the code backs its claims. Every claim must be verifiable from `/evidence/` or
by running one command. Feature breadth, framework variety, and scaling infrastructure are
explicit non-goals.

Design docs are the source of truth: `prd.md` (product scope, schema, milestones) and
`tech.md` (architecture, stack, free-tier strategy). Read the relevant section before changing
anything structural.

## The four architectural constraints

Violating any of these is a **bug**, not a style choice. Do not "temporarily" break one.

- **C1 — the session outlives the run.** A human takes control of the *same live session*, so runs
  *attach* to a browser held by `SessionRegistry`; they never launch or close their own. Any code
  that opens a browser inside a run violates C1.
- **C2 — one action chokepoint.** Every action from every source (discovery, replay, handoff resume)
  passes `PolicyEngine.check()` before touching a surface. Prompt-level guardrails are not
  guardrails. A second path to `surface.act()` violates C2.
- **C3 — no surface-specific locator is ever a primary strategy.** `anchor_relative` and
  `role_name` lead. `dom_hint` (css/xpath) is a terminal fallback only, flagged
  `surface_specific: true`, capped at score 0.3, and skipped by non-web resolvers. This seam is
  what makes the desktop story credible.
- **C4 — replay makes zero model calls.** Enforced structurally: `ReplayEngine` takes no
  `LLMClient` in its constructor and none reachable through its dependency graph. A test asserts
  this. Type-level guarantee, not a convention.

## Stack

- Python 3.11+, `uv` for packaging and running.
- Pydantic v2 for every model that is persisted or crosses a boundary; JSON Schema export is the
  agent-facing contract.
- Playwright (Python), headful, accessibility tree as the primary observation channel and
  screenshots strictly secondary.
- **Gemini free tier** (Flash) — the vision/discovery loop. Billing must stay disabled on the
  project; enabling it deletes the free tier.
- **Groq free tier** — text-only passes (outcome proposal), to preserve Gemini quota.
- Ollama is an offline fallback for iterating on plumbing, never for producing evidence.
- Storage is the local filesystem: JSON artifacts, JSONL logs. Nothing is deployed.

Rate limits, not cost, are the binding constraint: ~15 RPM. Every LLM call goes through the token
bucket limiter with exponential backoff, and RPD exhaustion fails the run loudly instead of
retrying for hours.

## Module layout

```
src/cua/          # package root; import path is cua.replay.engine, etc.
  surfaces/   base.py (Surface protocol: observe()->Observation, act(Action)->ActionResult)
              web.py (WebSurface: Playwright, AX primary), desktop.py (interface only,
              raises NotImplementedError), pruning.py (AX tree reduction)
  session/    registry.py (SessionRegistry, long-lived headful browsers)
              lock.py (ControlLock: automation | human | none)
  policy/     engine.py (PolicyEngine.check — the chokepoint)
              rules.py (allowlist + risk classification), redaction.py (log/artifact filter)
  llm/        base.py (LLMClient protocol), gemini.py, groq.py, ollama.py
              limiter.py (token bucket, backoff, 429 discrimination)
  discovery/  runner.py (observe->decide->act loop, stopping conditions)
              tools.py (closed action tool schema), prompts.py
  recording/  synthesizer.py (LocatorSynthesizer: candidates + uniqueness verification)
              outcomes.py (outcome-proposal pass + human approval gate)
  schema/     models.py (Capability, Step, ControlDescriptor, Locator, Outcome, ...)
              export.py (JSON Schema export)
  replay/     engine.py (deterministic executor, no LLMClient)
              resolver.py (candidate chain, drift signalling), conditions.py
  escalation/ intervention.py, handoff.py (lock release, capture, resume, re-verification)
              operator_app.py (mocked console: FastAPI + static HTML)
  catalog/    catalog.py (capability discovery + typed invocation by name)
  evidence/   logger.py (JSONL writer with redaction filter)
apps/harness/   fault-injection app, tenant-a and tenant-b
capabilities/   saved artifacts + overlays
evidence/       gitignored: run output may contain captured page state
tests/
```

Entry points (single process, no queue, no worker pool):

```
uv run cua discover --goal "..." --target <url> --tenant tenant-a
uv run cua replay --capability <id> --params '{...}' [--tenant tenant-b] [--offline]
uv run cua review | stability | operator
```

## Conventions

- Full type annotations on every function, including returns. No untyped `dict` crossing a
  module boundary.
- Pydantic v2 models for all persisted data and all inter-module contracts. If it hits disk or an
  API, it has a model.
- **No bare `except`.** Catch the specific exception; if you must catch broadly, catch `Exception`,
  log it structured, and re-raise or convert to a typed failure.
- Structured **JSONL** logging only, through `evidence/logger.py` (which applies the redaction
  filter). **No `print()` in library code** — CLI entry points may write to stdout.
- Errors are classified, never conflated: `business` outcomes return `status="business_outcome"`
  as a normal result, `recoverable` are handled and continued past, `hard_failure` stops with
  evidence. Returning a business outcome as an exception is the mistake this project exists to not make.

## Hard rules

- **Never commit secrets.** Keys live in `.env` (gitignored); `.env.example` holds names only.
  CI makes no LLM calls and needs no secrets.
- **Never write sensitive parameter values to artifacts, logs, stability records, or evidence.**
  Params marked `sensitive` are supplied per invocation and dropped on the way out; the redaction
  filter is the backstop, not the plan. Screenshots default to **off** (`--allow-screenshots`,
  synthetic targets only) because OCR redaction is not in this budget.
- Synthetic data only, always. Free-tier prompts leave the machine and are retained by the
  provider. No real PII, credentials, or institution names in any prompt.
- **Never add a dependency without justifying it** in the PR/commit message: what it replaces,
  why stdlib or an installed package cannot do it, and its license/cost (this project is $0, no card).
- Irreversible actions (delete / transfer / close / disburse / wire) are **blocked and escalated**,
  never model-confirmed. A model that can approve its own risky action is not a control.
