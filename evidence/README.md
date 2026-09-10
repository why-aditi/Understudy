# Evidence

Runs against `apps/harness`, which is synthetic throughout — invented members, invented
institution names, no real PII anywhere. Every run here was produced by the commands in the
root README and can be reproduced with them.

`evidence/` is gitignored by default, because in production run output would carry captured
page state. The set below is committed deliberately, listed by name in `.gitignore` so that
what is published is a decision rather than an accident.

## The end-to-end thread

**`discovery-20260910T211157-fb8cb8/`** — one real LLM-driven run (`gpt-oss-120b`, Groq free
tier) that reached its goal in four steps.

| | |
|---|---|
| `run.jsonl` | one record per step: observation hash, pruning ratio, the model's stated reasoning, the proposed action, the policy verdict, the result, elapsed time |
| `ax-snapshots/` | the pruned accessibility tree the model actually reasoned over, per step — this is the input to every decision, and unlike a screenshot it costs nothing to share |
| `screenshots/` | captured because the run passed `--allow-screenshots`; off by default |
| `capability.json` | the draft artifact the run emitted, assembled from the trace |

That artifact replays deterministically, with no model in the loop:

```bash
uv run cua replay --capability evidence/discovery-20260910T211157-fb8cb8/capability.json --attended
```

`--attended` because it is a **draft**: `state=draft`, no outcomes, every candidate
`verified_unique_at_record=false`. The model chose those controls and no human has agreed they
are the right ones, so it may not replay unattended.

## One replay per outcome class

The three-class taxonomy is the distinction the result contract exists for, so there is a run
for each. Which class a run was is in `result.json`'s `status`, not the directory name — the
class is only known once the run ends.

| directory | `status` | what it shows |
|---|---|---|
| `replay-business-not-found/` | `business_outcome` | "no such member" returned as a legitimate answer with a named outcome, **not** an exception |
| `replay-recoverable-modal/` | `success` | an unexpected interstitial detected, recovered from, and recorded in `drift_signals` |
| `replay-hard-failure-permission/` | `failure` | stopped with `FailureDetail` — the step, what was expected, what was observed, every candidate tried — plus `failure-read-balance.ax.json`, the accessibility snapshot of the screen it died on |

## The escalation transfer

**`handoff-live-demo/`** — a run that stopped at the frameset detail screen, wrote its
intervention, released the lock to a human, captured what they did, and re-verified the step's
checkpoint on resume.

| | |
|---|---|
| `intervention.json` | why it stopped, which capability and step, the url, the lock state, and who resumed it |
| `intervention-open-savings.ax.json` | the accessibility snapshot the operator console showed |
| `run.jsonl` | `escalated`, `intervention_written`, `lock_released`, `human_capture_started`, `human_action`, `resumed`, `resume_reverified` — in that order |

Two actions were captured: the click, and the navigation it caused. The click records
`frame: "accounts"`, and that is the point — capture is injected per frame and re-injected on
every navigation, and a listener on the top document alone would have recorded nothing while
appearing to work. Typed values never cross the boundary: the page-side listener reports
`value_length` and nothing else.

Regenerate it with `uv run python scripts/handoff_demo.py` against the running harness. The
human's clicks are driven through Playwright, since nobody is at the keyboard — but they are
dispatched to the page and seen by the listener like anyone's. The stand-in is the mouse, not
the mechanism.

## Stability

**`stability/replay-x10.json`** and **`stability/drift-x10.json`** — `member.search` replayed
ten times: 10/10, deterministic, every control resolving through its recorded primary, and a
drift verdict with no signals. Replay costs no model calls, which is the only reason measuring
determinism ten times over is affordable.

## The four standalone proofs

Each is generated from a real run rather than written by hand.

| file | claim it backs |
|---|---|
| `desktop-ax-proof.txt` | recorded locator candidates resolve against a native Windows UI Automation tree through the unmodified resolver; `dom_hint` is the only strategy skipped |
| `tenant-overlay-proof.txt` | one capability recorded on tenant-a replays on tenant-b through an overlay alone, no re-recording — plus a deliberately incomplete overlay that drift detection catches |
| `catalog-agent-demo.txt` | an LLM picks a capability by name, calls it with typed args, and gets a `ReplayResult` back — with model calls during replay measured at zero |
| `redaction-proof.txt` | a sensitive parameter reaches the surface and never reaches disk |

## What is not here

Every other run this project produced. They are reproducible rather than checked in: the run
ids quoted in `README.md` and `REPORT.md` name directories on the machine that produced them.
No screen recording — optional in the brief, and the accessibility snapshots carry more.
