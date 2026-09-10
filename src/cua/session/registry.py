"""SessionRegistry: owns long-lived headful browser sessions that outlive any single run (C1)."""

import logging
from types import TracebackType
from typing import Self

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from cua.surfaces.web import DEFAULT_HEADLESS

_log = logging.getLogger(__name__)


class SessionRegistry:
    """Holds browsers keyed by session id. Runs attach to a session; they never own one.

    A run that launched its own browser would close it on exit, and requirement 3.6 needs
    a human to take over the same live window (C1).
    """

    # ponytail: in-process only. Surviving across CLI invocations needs the operator
    # daemon to hold the registry; the handoff demo runs inside one process until then.
    def __init__(self, *, headless: bool = DEFAULT_HEADLESS) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, Page] = {}

    def attach(self, session_id: str) -> Page:
        """Return the page for a session, opening one on first use."""
        if session_id not in self._pages:
            context = self._browser_instance().new_context()
            self._contexts[session_id] = context
            self._pages[session_id] = context.new_page()
            _log.info("session_opened", extra={"session_id": session_id, "headless": self.headless})
        return self._pages[session_id]

    def _browser_instance(self) -> Browser:
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
        return self._browser

    def close(self, session_id: str) -> None:
        context = self._contexts.pop(session_id, None)
        self._pages.pop(session_id, None)
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

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close_all()
