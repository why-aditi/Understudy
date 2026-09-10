"""C1: the session outlives the run, and whoever drives it holds the lock."""

from typing import cast

import pytest
from playwright.sync_api import Page

from cua.session.lock import ControlLock, LockError, LockState
from cua.session.registry import SessionError, SessionRegistry
from cua.surfaces.base import Action, ActionTarget
from cua.surfaces.web import WebSurface
from test_web_surface import FakePage

# ---- the lock ---------------------------------------------------------------------------


def test_a_fresh_lock_is_held_by_nobody() -> None:
    lock = ControlLock("s1")
    assert lock.holder == "none"
    assert not lock.held
    assert lock.acquired_at is None


def test_acquiring_records_who_and_when() -> None:
    lock = ControlLock("s1")
    lock.acquire("automation", by="discovery-run-7")

    assert lock.holder == "automation"
    assert lock.acquired_by == "discovery-run-7"
    assert lock.acquired_at is not None
    assert lock.held


def test_acquiring_a_held_lock_fails_loudly() -> None:
    """Two parties sharing one browser is the bug this exists to make impossible."""
    lock = ControlLock("s1")
    lock.acquire("automation", by="run-a")

    with pytest.raises(LockError, match="already held by automation"):
        lock.acquire("human", by="operator")

    assert lock.holder == "automation", "a failed acquire must not disturb the holder"
    assert lock.acquired_by == "run-a"


def test_even_the_same_holder_cannot_re_acquire() -> None:
    """Not re-entrant on purpose: a second acquire means someone never released."""
    lock = ControlLock("s1")
    lock.acquire("automation", by="run-a")
    with pytest.raises(LockError, match="already held"):
        lock.acquire("automation", by="run-a")


def test_release_then_acquire_is_how_control_transfers() -> None:
    lock = ControlLock("s1")
    lock.acquire("automation", by="run-a")
    lock.release()
    assert lock.holder == "none"

    lock.acquire("human", by="operator")
    assert lock.held_by("human")
    assert not lock.held_by("automation")


def test_releasing_an_unheld_lock_is_not_an_error() -> None:
    """It is already the goal state, and handoff code should not have to check first."""
    lock = ControlLock("s1")
    lock.release()
    assert lock.holder == "none"


def test_you_cannot_acquire_for_nobody() -> None:
    with pytest.raises(LockError, match="use release"):
        lock = ControlLock("s1")
        lock.acquire("none", by="confused")


def test_require_names_who_actually_holds_it() -> None:
    lock = ControlLock("s1")
    lock.acquire("human", by="operator")

    with pytest.raises(LockError, match="held by human"):
        lock.require("automation")
    lock.require("human")


def test_a_lock_snapshots_for_evidence() -> None:
    lock = ControlLock.for_automation("s1", by="run-a")
    state = lock.snapshot()

    assert isinstance(state, LockState)
    assert state.session_id == "s1"
    assert state.holder == "automation"
    assert LockState.model_validate(state.model_dump(mode="json")) == state


# ---- every act() checks the lock -----------------------------------------------------------


def click() -> Action:
    return Action(kind="click", target=ActionTarget(role="button", name="Search"))


def test_a_surface_without_a_lock_can_observe_but_never_act() -> None:
    """Stricter than a default-open lock: there is no way to act without holding control."""
    page = FakePage()
    surface = WebSurface(cast(Page, page))

    assert surface.observe().url == page.url

    with pytest.raises(LockError, match="read-only"):
        surface.act(click())
    assert page.calls == [], "nothing may reach the page"


def test_automation_cannot_act_while_a_human_holds_the_lock() -> None:
    """The handoff case: an operator is mid-correction and the run must keep its hands off."""
    page = FakePage()
    lock = ControlLock("s1")
    lock.acquire("human", by="operator")
    surface = WebSurface(cast(Page, page), lock)

    with pytest.raises(LockError, match="held by human"):
        surface.act(click())
    assert page.calls == []


def test_automation_cannot_act_after_releasing_the_lock() -> None:
    page = FakePage()
    lock = ControlLock.for_automation("s1")
    surface = WebSurface(cast(Page, page), lock)

    assert surface.act(click()).ok
    lock.release()

    with pytest.raises(LockError, match="held by none"):
        surface.act(click())


def test_the_check_happens_before_the_action_is_even_inspected() -> None:
    """A malformed action is normally ok=False; without the lock it never gets that far."""
    page = FakePage()
    surface = WebSurface(cast(Page, page), ControlLock("s1"))

    with pytest.raises(LockError):
        surface.act(Action(kind="click"))  # no target: would otherwise be a failed result


# ---- the registry owns; runs attach ---------------------------------------------------------


class FakeRegistry(SessionRegistry):
    """A registry whose browser is a stub, so ownership can be tested without launching one."""

    def __init__(self) -> None:
        super().__init__(headless=True)
        self.closed: list[str] = []

    def _browser_instance(self) -> object:  # type: ignore[override]
        registry = self

        class _Context:
            def new_page(self) -> FakePage:
                return FakePage()

            def close(self) -> None:
                registry.closed.append("context")

        class _Browser:
            def new_context(self) -> _Context:
                return _Context()

        return _Browser()


def test_attaching_to_a_session_that_was_never_opened_fails() -> None:
    """C1 in one method: a run asks for a session and gets an error, not a fresh browser."""
    registry = FakeRegistry()
    with pytest.raises(SessionError, match="no session 'run-1' to attach to"):
        registry.attach("run-1")


def test_a_run_attaches_to_the_session_the_owner_opened() -> None:
    registry = FakeRegistry()
    opened = registry.open("run-1")
    attached = registry.attach("run-1")

    assert attached is opened, "attaching must return the same live session, not a copy"
    assert attached.lock.holder == "none", "the session starts unclaimed"
    assert registry.sessions == ["run-1"]


def test_opening_the_same_session_twice_fails() -> None:
    registry = FakeRegistry()
    registry.open("run-1")
    with pytest.raises(SessionError, match="already open"):
        registry.open("run-1")


def test_a_session_survives_the_run_that_used_it() -> None:
    """The point of C1: the run finishes and the browser is still there for the next party."""
    registry = FakeRegistry()
    session = registry.open("run-1")
    session.lock.acquire("automation", by="run-1")

    # ... the run happens, and ends by releasing control rather than closing anything.
    session.lock.release()

    still_there = registry.attach("run-1")
    assert still_there is session
    assert registry.closed == [], "a run must never close the session it attached to"


def test_closing_is_the_owners_job() -> None:
    registry = FakeRegistry()
    registry.open("run-1")
    registry.close("run-1")

    assert registry.sessions == []
    assert registry.closed == ["context"]
    with pytest.raises(SessionError):
        registry.attach("run-1")


def test_two_sessions_are_independent() -> None:
    registry = FakeRegistry()
    first = registry.open("run-1")
    second = registry.open("run-2")

    first.lock.acquire("automation", by="run-1")

    assert second.lock.holder == "none", "one run's lock must not affect another session"
    assert registry.sessions == ["run-1", "run-2"]
