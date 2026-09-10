"""Hand a live session to a human and take it back, against the running harness.

Escalation is the one core requirement whose evidence a reviewer could previously read but
not regenerate: the desktop proof and the agent demo ship as scripts, the tenant-overlay and
redaction proofs reproduce from documented CLI commands, and this reproduced from nothing.
That also meant it could not be re-verified after a change to the surface. This script closes
that.

What is real here and what stands in for a person:

  real     the intervention written before the lock is released, in that order
  real     the lock transfer, and that automation cannot act while a human holds it
  real     the per-frame capture, re-injected on navigation, values never crossing
  real     the resume signal, and the checkpoint re-verified afterwards
  stand-in the human's clicks, driven through Playwright rather than by a hand on a mouse

The stand-in is the mouse, not the mechanism. The clicks travel the same path a person's
would - they are dispatched to the page, and the capture listener observes them exactly as it
would observe anyone. Nothing here calls the capture directly.

    uv run python -m apps.harness 8099
    uv run python scripts/handoff_demo.py
"""

import sys

from cua.escalation.handoff import Handoff
from cua.escalation.intervention import signal_resume
from cua.evidence.logger import RunLogger
from cua.replay.conditions import describe
from cua.schema.models import Condition
from cua.session.registry import SessionRegistry
from cua.surfaces.base import Action
from cua.surfaces.web import WebSurface

RUN_ID = "handoff-live-demo"
BASE = "http://127.0.0.1:8099/tenant-a"
MEMBER = "12345"

# The detail screen is a frameset. Its top document exposes no accessibility tree at all, so
# a run that gets here has nothing to resolve against - which is precisely the shape of
# "stuck" that a human has to unstick.
DETAIL = f"{BASE}/members/{MEMBER}"

# Where the run expects to be once the human is done: inside the savings sub-account.
CHECKPOINT = Condition(kind="text_present", params={"text": "Savings"})


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    with RunLogger(RUN_ID) as log, SessionRegistry(headless=True) as sessions:
        sessions.open(RUN_ID)
        session = sessions.attach(RUN_ID)
        session.lock.acquire("automation", by=RUN_ID)
        surface = WebSurface(session.page, session.lock)
        surface.act(Action(kind="navigate", value=DETAIL))

        handoff = Handoff(surface=surface, lock=session.lock, logger=log, page=session.page)

        print(f"lock before escalating : {session.lock.holder}")
        request = handoff.escalate(
            step_id="open-savings",
            reason="stuck",
            message="The detail screen is a frameset; no candidate resolved.",
            capability_id="member.savings_balance.read",
            capability_version="1.0.0",
            goal="Open the member's savings sub-account.",
        )
        print(f"intervention written   : {request.reason} at step {request.step_id!r}")
        print(f"lock after escalating  : {session.lock.holder}  <- automation cannot act now")

        # --- the human ---------------------------------------------------------------------
        # Driven through Playwright because there is nobody at the keyboard. The clicks are
        # dispatched to the page and observed by the capture listener like any others; the
        # frame each lands in is what the record has to get right, because a listener on the
        # top document alone would see none of them.
        accounts = next(f for f in session.page.frames if f.name == "accounts")
        accounts.get_by_role("link", name="Open").first.click()
        session.page.wait_for_timeout(400)

        signal_resume(log.directory, by="operator")
        actions = handoff.resume(by="operator")
        print(f"lock after resuming    : {session.lock.holder}")
        print(f"human actions captured : {len(actions)}")
        for action in actions:
            print(f"    {action.describe()}   frame={action.frame!r}")
            if action.value_length is not None:
                print(f"      typed value: {action.value_length} characters, never the value")

        held = handoff.reverify(CHECKPOINT)
        print(f"checkpoint re-verified : {describe(CHECKPOINT)} -> {held}")
        print()
        print(f"evidence: {log.directory}")


if __name__ == "__main__":
    main()
