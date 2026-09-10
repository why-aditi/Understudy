"""Same-session handoff: release the lock, capture human actions, resume, re-verify.

The mocked part of this project is the operator's console. Everything here is real: the
request on disk, the lock changing hands, the capture of what the human did, the resume, and
the re-verification afterwards.

That last step is the one worth arguing for. When a human takes over a stuck run they will
often do more than the run was about to do - fix the record, navigate somewhere else, come
back. Resuming as though nothing moved would replay the next step against a screen it was
never recorded on, so the checkpoint is re-asserted before anything else happens.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Frame, Page

from cua.escalation.intervention import (
    HumanAction,
    InterventionRequest,
    Reason,
    ResumeSignal,
    clear_resume,
    read_request,
    resume_signal,
    write_request,
)
from cua.evidence.logger import RunLogger
from cua.replay.conditions import describe, evaluate
from cua.schema.models import Condition
from cua.session.lock import ControlLock
from cua.surfaces.base import Surface

_log = logging.getLogger(__name__)

# Installed into every frame, on every navigation. The page-side half deliberately reports
# the *length* of an input rather than its content: a value that never leaves the browser
# cannot be leaked by a redaction bug downstream.
CAPTURE_SCRIPT = """
() => {
  // The guard lives on <html>, not on window. document.write() and document.open() replace
  // the document - wiping every listener - without firing a navigation, and a window-level
  // flag would survive that and make re-injection a no-op. Legacy apps write documents.
  const root = document.documentElement;
  if (!root || root.__cuaCaptureInstalled) return;
  root.__cuaCaptureInstalled = true;

  const describe = (el) => {
    if (!el || !el.tagName) return { role: null, name: null };
    const tag = el.tagName.toLowerCase();
    const role =
      el.getAttribute('role') ||
      (tag === 'a' ? 'link'
        : tag === 'button' ? 'button'
        : tag === 'input' ? (el.type || 'textbox')
        : tag === 'select' ? 'combobox'
        : tag);
    let name = el.getAttribute('aria-label') || '';
    if (!name && el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label) name = label.textContent || '';
    }
    if (!name && el.name) name = el.name;
    if (!name) name = (el.innerText || el.value || '').slice(0, 60);
    return { role: String(role), name: name.trim().slice(0, 80) || null };
  };

  const send = (payload) => {
    try { window.__cuaRecord(payload); } catch (e) { /* the run has stopped listening */ }
  };

  document.addEventListener('click', (e) => {
    send({ kind: 'click', ...describe(e.target) });
  }, true);

  document.addEventListener('change', (e) => {
    const el = e.target;
    const typed = (el && typeof el.value === 'string') ? el.value.length : null;
    send({ kind: 'input', ...describe(el), value_length: typed });
  }, true);

  document.addEventListener('submit', (e) => {
    send({ kind: 'submit', ...describe(e.target) });
  }, true);
}
"""

BINDING = "__cuaRecord"

# A page-side binding can only be registered once per page, and a second registration fails.
# Without a dispatcher, a run that escalates twice would re-register, lose, and quietly send
# every event to the first (already stopped) capture - recording nothing while appearing to
# work. So the binding is installed once and forwards to whichever capture is live now.
_ACTIVE: dict[int, "HumanCapture"] = {}
_BOUND: set[int] = set()


def _dispatch(source: dict[str, Any], payload: dict[str, Any]) -> None:
    capture = _ACTIVE.get(id(source.get("page")))
    if capture is not None:
        capture.handle(source, payload)


class HandoffError(Exception):
    """The handoff is not in a state this operation makes sense in."""


class HumanCapture:
    """Records what a human does to the session while they hold the lock.

    Injection is per frame and re-applied on every navigation, which is not a nicety: the
    harness's detail screen is a frameset, and a listener installed only on the top document
    would record nothing at all while appearing to work perfectly.
    """

    def __init__(self, page: Page, logger: RunLogger | None = None) -> None:
        self.page = page
        self.logger = logger
        self.actions: list[HumanAction] = []
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        _ACTIVE[id(self.page)] = self
        if id(self.page) not in _BOUND:
            self.page.expose_binding(BINDING, _dispatch)
            _BOUND.add(id(self.page))
        # add_init_script runs in every frame of every document created from now on, which
        # is what makes this survive both navigation and frameset children.
        self.page.add_init_script(CAPTURE_SCRIPT)
        self.page.on("framenavigated", self._on_navigation)
        self._running = True
        _log.info("human_capture_started", extra={"url": self.page.url})

        # Frames that already exist predate the init script, so they are injected directly.
        for frame in self.page.frames:
            self._inject(frame)

    def stop(self) -> None:
        if not self._running:
            return
        self.page.remove_listener("framenavigated", self._on_navigation)
        _ACTIVE.pop(id(self.page), None)
        self._running = False
        _log.info("human_capture_stopped", extra={"actions": len(self.actions)})

    def _inject(self, frame: Frame) -> None:
        try:
            frame.evaluate(CAPTURE_SCRIPT)
        except Exception as exc:  # noqa: BLE001 - a detached or cross-origin frame is normal
            _log.info(
                "capture_injection_skipped",
                extra={"frame": frame.url, "detail": str(exc)[:160]},
            )

    def _on_navigation(self, frame: Frame) -> None:
        if not self._running:
            return
        self._inject(frame)
        self._record(HumanAction(kind="navigate", url=frame.url, frame=frame.name or frame.url))

    def handle(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        """Called by the page-side binding, via the per-page dispatcher."""
        if not self._running:
            return
        frame = source.get("frame")
        self._record(
            HumanAction(
                kind=payload.get("kind", "click"),
                role=payload.get("role"),
                name=payload.get("name"),
                value_length=payload.get("value_length"),
                frame=getattr(frame, "name", "") or getattr(frame, "url", ""),
            )
        )

    def _record(self, action: HumanAction) -> None:
        self.actions.append(action)
        if self.logger is not None:
            # Human actions belong in the same run log as the automation's, in order.
            self.logger.event(
                "human_action",
                kind=action.kind,
                role=action.role,
                control=action.name,
                url=action.url,
                value_length=action.value_length,
                frame=action.frame,
            )


class Handoff:
    """Escalate, hand the session over, take it back, and check where it was left."""

    def __init__(
        self,
        *,
        surface: Surface,
        lock: ControlLock,
        logger: RunLogger,
        page: Page | None = None,
        allow_screenshots: bool = False,
    ) -> None:
        self.surface = surface
        self.lock = lock
        self.logger = logger
        self.allow_screenshots = allow_screenshots
        self.capture = HumanCapture(page, logger) if page is not None else None
        self.request: InterventionRequest | None = None

    # -- out ------------------------------------------------------------------------------

    def escalate(
        self,
        *,
        step_id: str,
        reason: Reason,
        message: str,
        capability_id: str | None = None,
        capability_version: str | None = None,
        goal: str | None = None,
    ) -> InterventionRequest:
        """Write the request, then release the lock. In that order, always.

        Releasing first would leave a window a human could take over with no record of why
        it was handed to them.
        """
        observation = self.surface.observe(screenshot=self.allow_screenshots)

        snapshot = self.logger.save_artifact(
            f"intervention-{step_id}.ax.json",
            observation.model_dump_json(indent=2).encode("utf-8"),
        )
        screenshot = None
        if observation.screenshot is not None:
            screenshot = str(
                self.logger.save_artifact(f"intervention-{step_id}.png", observation.screenshot)
            )

        request = InterventionRequest(
            run_id=self.logger.run_id,
            reason=reason,
            step_id=step_id,
            message=message,
            capability_id=capability_id,
            capability_version=capability_version,
            goal=goal,
            url=observation.url,
            ax_snapshot_path=str(snapshot),
            screenshot_ref=screenshot,
            lock=self.lock.snapshot(),
        )
        write_request(request, self.logger.directory)
        # Not "message": logging reserves it on LogRecord and raises if extra shadows it.
        self.logger.event(
            "escalated", reason=reason, step=step_id, detail=message, url=observation.url
        )

        self.lock.release()
        self.request = request
        if self.capture is not None:
            self.capture.start()
        return request

    # -- back -----------------------------------------------------------------------------

    def resume(self, *, by: str = "operator") -> list[HumanAction]:
        """Take the lock back for automation and stop watching.

        Returns what the human did, which is appended to the request on disk so the record
        of the handoff is complete rather than only half-written.
        """
        if self.request is None:
            raise HandoffError("resume called without an outstanding intervention")

        actions: list[HumanAction] = []
        if self.capture is not None:
            self.capture.stop()
            actions = list(self.capture.actions)

        self.lock.acquire("automation", by=self.logger.run_id)
        self.request = self.request.model_copy(
            update={
                "resolved_at": datetime.now(UTC),
                "resumed_by": by,
                "human_actions": actions,
            }
        )
        write_request(self.request, self.logger.directory)
        self.logger.event(
            "resumed",
            by=by,
            human_actions=len(actions),
            summary=[action.describe() for action in actions],
        )
        return actions

    def wait_for_resume(
        self,
        *,
        timeout: float = 900.0,
        poll: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ResumeSignal | None:
        """Block until an operator signals resume, or the wait runs out.

        Polling a file is not elegant, but the console is a separate process holding no
        reference to this run. Returning None on timeout rather than raising lets the caller
        decide whether an unattended run should give up or keep waiting.
        """
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            signal = resume_signal(self.logger.directory)
            if signal is not None:
                clear_resume(self.logger.directory)
                return signal
            sleep(poll)
        self.logger.event("resume_wait_timed_out", timeout_seconds=timeout)
        return None

    def reverify(self, checkpoint: Condition | None) -> bool:
        """Re-assert the step's checkpoint before the run continues.

        A human who took over a stuck run may have done anything at all - including fixing
        the problem somewhere else entirely and leaving the browser two screens away. The
        run does not get to assume the app is where it left it.
        """
        if checkpoint is None:
            return True
        observation = self.surface.observe()
        held = evaluate(checkpoint, observation.tree, observation.url)
        self.logger.event(
            "resume_reverified",
            checkpoint=describe(checkpoint),
            holds=held,
            url=observation.url,
        )
        return held


def pending(run_directory: Path) -> InterventionRequest | None:
    """The open intervention for a run, if it has one."""
    request = read_request(run_directory)
    return request if request is not None and request.open else None
