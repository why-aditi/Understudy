"""SessionRegistry: owns long-lived headful browser sessions that outlive any single run (C1).

Requirement 3.6 needs a human to take control of the *same live session* the automation was
driving. A run that launches its own browser and closes it on the way out cannot satisfy that
at any price, so the ownership is inverted: the registry owns browsers, and a run attaches to
one it did not create and must not close.

The split is deliberate and enforced by the API. `open()` creates a session and belongs to
whoever owns the registry - today the CLI entry point, tomorrow the operator daemon.
`attach()` only ever returns a session that already exists, so a run cannot accidentally
create one by asking for it.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from cua.session.lock import ControlLock
from cua.surfaces.web import DEFAULT_HEADLESS

_log = logging.getLogger(__name__)


class SessionError(Exception):
    """The session does not exist, or already does."""


@dataclass
class Session:
    """One live browser window, and the lock that says who may drive it."""

    id: str
    page: Page
    lock: ControlLock
    created_at: datetime

    def __repr__(self) -> str:
        return f"Session(id={self.id!r}, url={self.page.url!r}, lock={self.lock.holder!r})"


class SessionRegistry:
    """Holds browsers keyed by session id. Runs attach to a session; they never own one.

    # ponytail: in-process only. Surviving across CLI invocations needs the operator daemon
    # to hold the registry, which is the productionised form described in the report.
    """

    def __init__(self, *, headless: bool = DEFAULT_HEADLESS) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._sessions: dict[str, Session] = {}

    # -- ownership: only the registry's owner calls these ---------------------------------

    def open(self, session_id: str) -> Session:
        """Create a session. Raises if one already exists under that id."""
        if session_id in self._sessions:
            raise SessionError(f"session {session_id!r} is already open")
        context = self._browser_instance().new_context()
        self._contexts[session_id] = context
        session = Session(
            id=session_id,
            page=context.new_page(),
            lock=ControlLock(session_id),
            created_at=datetime.now(UTC),
        )
        self._sessions[session_id] = session
        _log.info(
            "session_opened",
            extra={"session_id": session_id, "headless": self.headless},
        )
        return session

    def close(self, session_id: str) -> None:
        """End a session. Only the owner does this; a run that closed one would break C1."""
        context = self._contexts.pop(session_id, None)
        self._sessions.pop(session_id, None)
        if context is not None:
            context.close()
            _log.info("session_closed", extra={"session_id": session_id})

    def close_all(self) -> None:
        for session_id in list(self._contexts):
            self.close(session_id)
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    # -- use: what a run is allowed to do -------------------------------------------------

    def attach(self, session_id: str) -> Session:
        """Return an existing session. Never creates one.

        This is the whole of C1 in one method: a run asks for a session it did not make, and
        gets an error rather than a fresh browser if it is not there.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(
                f"no session {session_id!r} to attach to; open() it first "
                f"(currently open: {sorted(self._sessions) or 'none'})"
            )
        _log.info("session_attached", extra={"session_id": session_id})
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    @property
    def sessions(self) -> list[str]:
        return sorted(self._sessions)

    def _browser_instance(self) -> Browser:
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
        return self._browser

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close_all()
