"""Mocked operator console serving the pending intervention over localhost.

WHAT IS MOCKED HERE, EXACTLY
    Only this file. It is a single static page that polls a JSON file on disk and posts a
    resume signal back. There is no authentication, no session list per operator, no audit
    of who opened what, no styling worth the name, and - the important one - **no view of
    the browser**. An operator using this console cannot see or drive the session from it.
    They read the accessibility snapshot the run captured, go and do whatever is needed in
    the headful window that is already open on the same machine, and then press Resume.

WHAT IS REAL, AND IS NOT MOCKED
    The ControlLock and its transfer between automation and human; the InterventionRequest
    written to disk before the lock is released; the per-frame capture of what the human
    did, with typed values never leaving the browser; the resume signal; and the
    re-verification of the step's checkpoint afterwards. Those live in `handoff.py` and
    `intervention.py` and are covered by tests against a real browser.

WHAT PRODUCTION WOULD BE INSTEAD
    Two shapes, both out of scope for this brief:

    1. A containerised browser exposed over noVNC/WebRTC. The session runs in a pod, the
       operator gets pixels and input in the console itself, and the lock becomes the thing
       that gates input injection rather than a note two humans agree to honour. This is the
       usual answer and it costs a video transport plus a per-session pod.

    2. A CDP relay behind an auth boundary. The console proxies Chrome DevTools Protocol to
       the operator's browser, so the console renders the real DOM rather than a screenshot.
       Cheaper than video and much harder to secure: a CDP endpoint is remote code execution
       on the session, so it needs an authenticated, per-session, expiring channel and a
       relay that refuses anything outside a command allowlist.

    Either way the interesting parts - who holds control, what the human did, whether the
    app is still where the run expected - are the parts already built here. The transport is
    the part that costs money.
"""

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from cua.discovery.prompts import render_tree
from cua.escalation.intervention import InterventionRequest, read_request, signal_resume
from cua.surfaces.base import AXNode

_log = logging.getLogger(__name__)

DEFAULT_EVIDENCE = Path("evidence")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Operator console</title>
<style>
 body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #16181d; }
 header { background: #003366; color: #fff; padding: 10px 16px; }
 header b { font-size: 15px; }
 header span { opacity: .75; margin-left: 10px; font-size: 12px; }
 main { padding: 16px; max-width: 1000px; }
 .card { background: #fff; border: 1px solid #d8dbe0; border-radius: 6px;
         padding: 14px 16px; margin-bottom: 14px; }
 .none { color: #5a6472; }
 .reason { display: inline-block; padding: 2px 8px; border-radius: 3px; font-weight: 600;
           font-size: 12px; background: #ffe8e0; color: #92310d; }
 dl { display: grid; grid-template-columns: 150px 1fr; gap: 4px 12px; margin: 12px 0; }
 dt { color: #5a6472; } dd { margin: 0; word-break: break-all; }
 pre { background: #10131a; color: #d7dae0; padding: 12px; border-radius: 4px;
       overflow: auto; max-height: 340px; font-size: 12px; }
 button { background: #0b6b3a; color: #fff; border: 0; border-radius: 4px;
          padding: 9px 18px; font-size: 14px; cursor: pointer; }
 button:disabled { background: #9aa3ad; cursor: default; }
 .done { color: #0b6b3a; font-weight: 600; }
</style></head>
<body>
<header><b>Operator console</b><span id="tick">polling…</span></header>
<main id="root"><p class="none">Loading…</p></main>
<script>
const esc = (s) => String(s ?? "").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function resume(runId) {
  const by = prompt("Resuming as:", "operator") || "operator";
  await fetch(`/api/interventions/${encodeURIComponent(runId)}/resume`,
              {method: "POST", headers: {"Content-Type": "application/json"},
               body: JSON.stringify({by})});
  refresh();
}

function render(items) {
  const root = document.getElementById("root");
  if (!items.length) {
    root.innerHTML = '<div class="card none">No run is waiting for a human right now.</div>';
    return;
  }
  root.innerHTML = items.map(i => `
    <div class="card">
      <span class="reason">${esc(i.reason)}</span>
      <p>${esc(i.message)}</p>
      <dl>
        <dt>run</dt><dd>${esc(i.run_id)}</dd>
        <dt>capability</dt>
        <dd>${esc(i.capability_id || "-")} ${esc(i.capability_version || "")}</dd>
        <dt>step</dt><dd>${esc(i.step_id)}</dd>
        <dt>url</dt><dd>${esc(i.url)}</dd>
        <dt>lock</dt><dd>${esc(i.lock_holder)}</dd>
        <dt>raised</dt><dd>${esc(i.requested_at)}</dd>
      </dl>
      <p>What the run could see when it stopped:</p>
      <pre>${esc(i.ax_tree)}</pre>
      ${i.open
        ? `<button onclick="resume('${esc(i.run_id)}')">Resume the run</button>`
        : `<p class="done">Resumed by ${esc(i.resumed_by)} ·
             ${i.human_action_count} action(s) captured</p>`}
    </div>`).join("");
}

async function refresh() {
  try {
    const res = await fetch("/api/interventions");
    render(await res.json());
    document.getElementById("tick").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("tick").textContent = "console unreachable";
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body></html>
"""


def _render_tree(request: InterventionRequest) -> str:
    """The accessibility snapshot as text - the same view the run itself was working from."""
    if not request.ax_snapshot_path:
        return "(no snapshot captured)"
    path = Path(request.ax_snapshot_path)
    if not path.exists():
        return f"(snapshot missing: {path})"
    try:
        observation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"(snapshot unreadable: {exc})"

    tree = observation.get("tree")
    if tree is None:
        return "(the run could see nothing at all - a frameset, or a blank document)"
    return render_tree(AXNode.model_validate(tree))


def _summarise(directory: Path) -> dict[str, Any] | None:
    request = read_request(directory)
    if request is None:
        return None
    return {
        "run_id": request.run_id,
        "reason": request.reason,
        "message": request.message,
        "step_id": request.step_id,
        "capability_id": request.capability_id,
        "capability_version": request.capability_version,
        "url": request.url,
        "requested_at": request.requested_at.isoformat(),
        "lock_holder": request.lock.holder if request.lock else "unknown",
        "ax_tree": _render_tree(request),
        "open": request.open,
        "resumed_by": request.resumed_by,
        "human_action_count": len(request.human_actions),
    }


def create_app(evidence_root: Path = DEFAULT_EVIDENCE) -> FastAPI:
    """The console. One page, one poll endpoint, one button."""
    app = FastAPI(title="Understudy operator console", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(PAGE)

    @app.get("/api/interventions")
    async def interventions() -> list[dict[str, Any]]:
        """Every run that has raised an intervention, newest first."""
        if not evidence_root.exists():
            return []
        found = [
            summary
            for directory in sorted(evidence_root.iterdir())
            if directory.is_dir() and (summary := _summarise(directory)) is not None
        ]
        # Open ones first, then most recently raised: an operator wants the work, not history.
        return sorted(found, key=lambda s: (not s["open"], s["requested_at"]), reverse=False)

    @app.get("/api/interventions/{run_id}")
    async def intervention(run_id: str) -> dict[str, Any]:
        summary = _summarise(evidence_root / run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"no intervention for run {run_id!r}")
        return summary

    @app.post("/api/interventions/{run_id}/resume")
    async def resume(run_id: str, body: dict[str, str] | None = None) -> dict[str, str]:
        """Hand control back. The waiting run is polling for this file.

        The console does not touch the lock itself: it has no reference to the session, and
        a console that could seize control without the run noticing would be a worse bug
        than the one this exists to solve.
        """
        directory = evidence_root / run_id
        if read_request(directory) is None:
            raise HTTPException(status_code=404, detail=f"no intervention for run {run_id!r}")
        by = (body or {}).get("by", "operator")
        signal_resume(directory, by)
        return {"run_id": run_id, "resumed_by": by}

    return app


def serve(
    host: str = "127.0.0.1", port: int = 8765, evidence_root: Path = DEFAULT_EVIDENCE
) -> None:
    """Run the console. Localhost only, and deliberately so: there is no authentication."""
    import uvicorn

    uvicorn.run(create_app(evidence_root), host=host, port=port, log_level="warning")
