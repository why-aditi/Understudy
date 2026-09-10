"""Escalation and same-session handoff: the parts that are real rather than mocked.

The operator's console is out of scope. The lock changing hands, the request on disk, the
capture of what a human did, the resume and the re-verification are not.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from cua.escalation.handoff import BINDING, Handoff, HandoffError, HumanCapture, pending
from cua.escalation.intervention import (
    HumanAction,
    InterventionRequest,
    read_request,
    write_request,
)
from cua.evidence.logger import RunLogger
from cua.schema.models import Condition
from cua.session.lock import ControlLock
from cua.surfaces.web import WebSurface

# A frameset, because the harness's detail screen is one. A capture installed only on the
# top document records nothing here while looking like it works.
# No '#' anywhere in these data URLs: it is a fragment delimiter, so everything after it
# is silently dropped and the frame loads half the markup.
FRAMESET = """
<frameset rows="90,*">
  <frame name="summary" src="data:text/html,<h1>Member</h1><button id='ack'>Acknowledge</button>">
  <frame name="body" src="data:text/html,<label for='q'>Member ID</label>
<input id='q'><button id='go'>Open</button>">
</frameset>
"""

PLAIN = """
<h1>Member search</h1>
<label for="q">Member ID</label><input id="q">
<a href="#" id="go">Search</a>
"""


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser.new_context().new_page()
        browser.close()


def logger_for(tmp_path: Path, run_id: str = "handoff-test") -> RunLogger:
    return RunLogger(run_id, root=tmp_path)


def events(logger: RunLogger) -> list[dict[str, object]]:
    return [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]


# ---- capture: per frame, and surviving navigation -----------------------------------------


def test_capture_records_a_click_on_the_top_document(page: Page, tmp_path: Path) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    capture = HumanCapture(page)
    capture.start()

    page.click("#go")
    capture.stop()

    clicks = [a for a in capture.actions if a.kind == "click"]
    assert clicks, "no click captured"
    assert clicks[0].role == "link"
    assert clicks[0].name == "Search"


def test_capture_reaches_inside_a_frameset(page: Page, tmp_path: Path) -> None:
    """The warning in the brief made real: single-frame injection misses everything here."""
    page.set_content(FRAMESET, wait_until="domcontentloaded")
    capture = HumanCapture(page)
    capture.start()

    body = next(f for f in page.frames if f.name == "body")
    summary = next(f for f in page.frames if f.name == "summary")
    body.click("#go")
    summary.click("#ack")
    capture.stop()

    clicked = [(a.name, a.frame) for a in capture.actions if a.kind == "click"]
    assert ("Open", "body") in clicked
    assert ("Acknowledge", "summary") in clicked


def test_capture_survives_a_navigation(page: Page, tmp_path: Path) -> None:
    """A listener installed once on the first document would go quiet after the first click."""
    capture = HumanCapture(page)
    page.set_content(PLAIN, wait_until="domcontentloaded")
    capture.start()

    page.goto("data:text/html,<button id='later'>Continue</button>", wait_until="domcontentloaded")
    page.click("#later")
    capture.stop()

    names = [a.name for a in capture.actions if a.kind == "click"]
    assert "Continue" in names, "capture did not survive the navigation"
    assert any(a.kind == "navigate" for a in capture.actions)


def test_typed_values_never_leave_the_browser(page: Page, tmp_path: Path) -> None:
    """Only the length crosses the boundary, so there is no redaction step to forget."""
    page.set_content(PLAIN, wait_until="domcontentloaded")
    logger = logger_for(tmp_path)
    capture = HumanCapture(page, logger)

    with logger:
        capture.start()
        page.fill("#q", "998877-SECRET")
        page.click("#go")  # blur, so the change event fires
        capture.stop()

    typed = [a for a in capture.actions if a.kind == "input"]
    assert typed, "no input captured"
    assert typed[0].value_length == len("998877-SECRET")
    assert typed[0].name == "Member ID"

    written = logger.path.read_text(encoding="utf-8")
    assert "998877-SECRET" not in written
    assert "SECRET" not in written


def test_human_actions_land_in_the_run_log(page: Page, tmp_path: Path) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    logger = logger_for(tmp_path)
    capture = HumanCapture(page, logger)

    with logger:
        capture.start()
        page.click("#go")
        capture.stop()

    human = [e for e in events(logger) if e["event"] == "human_action"]
    assert human, "human actions must append to the same run log as the automation's"
    assert human[0]["kind"] == "click"


def test_capture_stops_recording_once_stopped(page: Page, tmp_path: Path) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    capture = HumanCapture(page)
    capture.start()
    capture.stop()

    page.click("#go")
    assert [a for a in capture.actions if a.kind == "click"] == []


def test_the_binding_name_is_what_the_page_script_calls() -> None:
    from cua.escalation.handoff import CAPTURE_SCRIPT

    assert f"window.{BINDING}(" in CAPTURE_SCRIPT


# ---- escalation: request first, then release ----------------------------------------------


def handoff_for(page: Page, tmp_path: Path, *, run_id: str = "handoff-test") -> Handoff:
    lock = ControlLock.for_automation(run_id, by=run_id)
    return Handoff(
        surface=WebSurface(page, lock),
        lock=lock,
        logger=logger_for(tmp_path, run_id),
        page=page,
    )


def test_escalating_writes_the_request_then_releases_the_lock(page: Page, tmp_path: Path) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)
    assert handoff.lock.holder == "automation"

    with handoff.logger:
        request = handoff.escalate(
            step_id="open-savings",
            reason="risky_action_blocked",
            message="The policy refused a Close account click.",
            capability_id="member.savings_balance.read",
        )

    assert not handoff.lock.held, "the human cannot take over a lock we still hold"
    on_disk = read_request(handoff.logger.directory)
    assert on_disk is not None
    assert on_disk.reason == "risky_action_blocked"
    assert on_disk.step_id == "open-savings"
    assert on_disk.open
    # The lock snapshot records the state as it was *before* the release.
    assert request.lock is not None and request.lock.holder == "automation"


def test_the_request_carries_an_ax_snapshot_of_the_stuck_screen(page: Page, tmp_path: Path) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)

    with handoff.logger:
        request = handoff.escalate(
            step_id="s1", reason="stuck", message="No progress for three observations."
        )

    assert request.ax_snapshot_path is not None
    snapshot = json.loads(Path(request.ax_snapshot_path).read_text(encoding="utf-8"))
    assert snapshot["tree"] is not None
    assert "Member search" in json.dumps(snapshot["tree"])


def test_no_screenshot_reference_unless_screenshots_are_permitted(
    page: Page, tmp_path: Path
) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)

    with handoff.logger:
        request = handoff.escalate(step_id="s1", reason="stuck", message="stuck")

    assert request.screenshot_ref is None


def test_a_screenshot_reference_is_recorded_when_permitted(page: Page, tmp_path: Path) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    lock = ControlLock.for_automation("r1")
    handoff = Handoff(
        surface=WebSurface(page, lock),
        lock=lock,
        logger=logger_for(tmp_path),
        page=page,
        allow_screenshots=True,
    )

    with handoff.logger:
        request = handoff.escalate(step_id="s1", reason="stuck", message="stuck")

    assert request.screenshot_ref is not None
    assert Path(request.screenshot_ref).exists()


# ---- the human drives, then hands back -------------------------------------------------------


def test_the_full_handoff_round_trip(page: Page, tmp_path: Path) -> None:
    """Escalate, a human fixes something, resume, and the record is complete."""
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)

    with handoff.logger:
        handoff.escalate(step_id="s1", reason="stuck", message="No progress.")
        assert not handoff.lock.held

        # The human takes control and does something.
        handoff.lock.acquire("human", by="operator")
        page.fill("#q", "12345")
        page.click("#go")
        handoff.lock.release()

        actions = handoff.resume(by="operator")

    assert handoff.lock.held_by("automation"), "resume returns the lock to automation"
    assert any(a.kind == "input" for a in actions)
    assert any(a.kind == "click" for a in actions)

    closed = read_request(handoff.logger.directory)
    assert closed is not None
    assert not closed.open
    assert closed.resumed_by == "operator"
    assert len(closed.human_actions) == len(actions)
    assert pending(handoff.logger.directory) is None


def test_resume_without_an_escalation_is_an_error(page: Page, tmp_path: Path) -> None:
    handoff = handoff_for(page, tmp_path)
    with pytest.raises(HandoffError, match="without an outstanding intervention"):
        handoff.resume()


def test_resume_fails_if_the_human_never_gave_the_lock_back(page: Page, tmp_path: Path) -> None:
    """Acquiring a held lock fails loudly, so a half-finished handoff cannot be papered over."""
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)

    with handoff.logger:
        handoff.escalate(step_id="s1", reason="stuck", message="stuck")
        handoff.lock.acquire("human", by="operator")

        with pytest.raises(Exception, match="already held by human"):
            handoff.resume()


# ---- re-verification: do not assume the app is where we left it -------------------------------


def test_reverify_passes_when_the_human_left_us_where_we_expected(
    page: Page, tmp_path: Path
) -> None:
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)
    checkpoint = Condition(kind="text_present", params={"text": "Member search"})

    with handoff.logger:
        assert handoff.reverify(checkpoint) is True


def test_reverify_fails_when_the_human_wandered_off(page: Page, tmp_path: Path) -> None:
    """The whole reason this step exists: a human fixing a problem may end up anywhere."""
    page.set_content(PLAIN, wait_until="domcontentloaded")
    handoff = handoff_for(page, tmp_path)
    checkpoint = Condition(kind="text_present", params={"text": "Member search"})

    with handoff.logger:
        handoff.escalate(step_id="s1", reason="stuck", message="stuck")
        handoff.lock.acquire("human", by="operator")
        page.goto("data:text/html,<h1>Somewhere else entirely</h1>", wait_until="domcontentloaded")
        handoff.lock.release()
        handoff.resume()

        assert handoff.reverify(checkpoint) is False

    reverified = [e for e in events(handoff.logger) if e["event"] == "resume_reverified"]
    assert reverified and reverified[-1]["holds"] is False


def test_a_step_with_no_checkpoint_reverifies_trivially(page: Page, tmp_path: Path) -> None:
    handoff = handoff_for(page, tmp_path)
    with handoff.logger:
        assert handoff.reverify(None) is True


# ---- the request is a plain artifact -----------------------------------------------------------


def test_an_intervention_request_round_trips(tmp_path: Path) -> None:
    request = InterventionRequest(
        run_id="r1",
        reason="unrecoverable",
        step_id="s1",
        message="Unknown screen.",
        human_actions=[HumanAction(kind="click", role="link", name="Open", frame="body")],
    )
    write_request(request, tmp_path)
    assert read_request(tmp_path) == request


def test_reading_a_request_that_was_never_written_returns_none(tmp_path: Path) -> None:
    assert read_request(tmp_path) is None


def test_a_human_action_describes_itself_without_the_value() -> None:
    action = HumanAction(kind="input", role="textbox", name="Member ID", value_length=13)
    described = action.describe()
    assert "13 character(s)" in described
    assert "Member ID" in described
