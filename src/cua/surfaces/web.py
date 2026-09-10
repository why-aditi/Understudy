"""WebSurface: Playwright surface using the accessibility tree as primary observation channel."""

import logging
import time
from typing import Any, cast

from playwright.sync_api import CDPSession, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from cua.session.lock import ControlLock, LockError
from cua.surfaces.base import Action, ActionResult, ActionTarget, AXNode, Observation
from cua.surfaces.pruning import observation_hash, prune

_log = logging.getLogger(__name__)

# SessionRegistry launches browsers, not this class: a surface that owns its browser
# cannot hand the same live window to a human (C1). This is the default it launches with.
DEFAULT_HEADLESS = False

# Containers a `near` anchor can scope to, innermost first. Every accessibility tree
# models containment, so this list is not web-specific.
CONTAINER_ROLES = ("row", "listitem", "group", "article", "region", "form", "dialog", "table")

# Chromium reports layout tables under internal role names that no ARIA role engine knows.
# We observe and verify against the accessibility tree but act through role locators, so the
# two must share one vocabulary or a candidate can verify and then be unfindable. Playwright
# computes these roles straight from the HTML tag, so `<td>` is a cell either way.
ROLE_ALIASES = {
    "LayoutTable": "table",
    "LayoutTableRow": "row",
    "LayoutTableCell": "cell",
}

# Actions that cannot do anything without a control to act on.
_NEEDS_TARGET = frozenset({"click", "type", "select", "wait_for", "extract"})
_NEEDS_VALUE = frozenset({"navigate", "type", "select", "press_key"})


class WebSurface:
    """Drives an already-open Playwright page. Reads the AX tree; never a DOM selector (C3)."""

    def __init__(self, page: Page, lock: ControlLock | None = None) -> None:
        self._page = page
        self._cdp: CDPSession | None = None
        # A surface with no lock may observe but never act. That is stricter than a default
        # open lock would be: there is no way to act without someone holding control.
        self._lock = lock

    # ---- observe -------------------------------------------------------------

    def observe(self, *, screenshot: bool = False) -> Observation:
        raw = self._ax_tree()
        tree, stats = prune(raw)
        url = self._page.url
        _log.info(
            "ax_pruned",
            extra={
                "url": url,
                "nodes_before": stats.nodes_before,
                "nodes_after": stats.nodes_after,
                "pruning_ratio": round(stats.ratio, 3),
                "rows_collapsed": stats.rows_collapsed,
                "values_truncated": stats.values_truncated,
            },
        )
        return Observation(
            url=url,
            title=self._title(),
            tree=tree,
            observation_hash=observation_hash(url, tree),
            pruning=stats,
            screenshot=self._page.screenshot(type="png") if screenshot else None,
        )

    def _title(self) -> str | None:
        try:
            return self._page.title()
        except PlaywrightError:
            return None

    def _session(self) -> CDPSession:
        # ponytail: CDP means Chromium only. Cross-browser needs a parser for
        # locator.aria_snapshot()'s YAML; the runbook installs chromium, so not yet.
        if self._cdp is None:
            self._cdp = self._page.context.new_cdp_session(self._page)
            self._cdp.send("Accessibility.enable")
        return self._cdp

    def _ax_tree(self) -> AXNode | None:
        """Full accessibility tree over CDP, converted into the shared AXNode vocabulary."""
        payload: dict[str, Any] = self._session().send("Accessibility.getFullAXTree")
        nodes: list[dict[str, Any]] = payload.get("nodes") or []
        if not nodes:
            return None

        by_id = {node["nodeId"]: node for node in nodes}
        children = {child for node in nodes for child in node.get("childIds") or []}
        roots = [node for node in nodes if node["nodeId"] not in children]
        return _convert(roots[0] if roots else nodes[0], by_id)

    # ---- act -----------------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        started = time.monotonic()
        error: str | None = None
        extracted: str | None = None

        # C1: whoever is driving must hold the lock on this session. Checked before the
        # action is even inspected, so no path reaches the page without it.
        if self._lock is None:
            raise LockError(
                "this surface has no session lock and is read-only; attach it to a session"
            )
        self._lock.require("automation")

        if action.kind in _NEEDS_TARGET and action.target is None:
            error = f"{action.kind} requires a target"
        elif action.kind in _NEEDS_VALUE and action.value is None:
            error = f"{action.kind} requires a value"
        else:
            try:
                extracted = self._perform(action)
            except PlaywrightError as exc:
                # Playwright's TimeoutError subclasses Error, so this covers both a missing
                # control and a control that never became actionable.
                error = f"{type(exc).__name__}: {exc.message.splitlines()[0]}"

        return ActionResult(
            action=action,
            ok=error is None,
            url_after=self._page.url,
            duration_ms=int((time.monotonic() - started) * 1000),
            extracted=extracted,
            error=error,
        )

    def _perform(self, action: Action) -> str | None:
        timeout = float(action.timeout_ms)
        value = action.value or ""

        if action.kind == "navigate":
            self._page.goto(value, timeout=timeout)
            return None
        if action.kind == "press_key" and action.target is None:
            self._page.keyboard.press(value)
            return None

        target = action.target
        if target is None:  # unreachable for the remaining kinds; keeps mypy honest
            raise PlaywrightError(f"{action.kind} requires a target")
        locator = self._locate(target)

        if action.kind == "click":
            locator.click(timeout=timeout)
        elif action.kind == "type":
            locator.fill(value, timeout=timeout)
        elif action.kind == "select":
            locator.select_option(value, timeout=timeout)
        elif action.kind == "press_key":
            locator.press(value, timeout=timeout)
        elif action.kind == "wait_for":
            locator.wait_for(state="visible", timeout=timeout)
        elif action.kind == "extract":
            return locator.inner_text(timeout=timeout)
        return None

    def _locate(self, target: ActionTarget) -> Locator:
        """Role plus accessible name, optionally scoped to the container holding `near`.

        The same two facts a desktop AX API would give us, plus containment - which every
        accessibility tree models. No CSS, no XPath, no DOM traversal (C3).
        """
        # Playwright types role as a Literal of ARIA roles; ours arrives as a str from
        # the artifact, so the cast is the boundary between the two.
        if target.near is None:
            return self._by_role(self._page, target).nth(target.nth)

        best: Locator | None = None
        fewest = 0
        # Across every container role, not the first role that happens to match. Comparing
        # only within a role made CONTAINER_ROLES' order load-bearing: `cell near="Name"` on
        # the results screen stopped at `row`, because the header row holding "Name" has
        # columnheaders rather than cells, so the only matching row was the outer body row -
        # and the answer came back as a nav link. The table one level out is tighter and
        # holds the right cells. This is the same tightest-valid-container rule
        # `synthesizer.find_target` applies to the accessibility tree; the two resolvers
        # disagreeing on one target is what a recorded descriptor cannot survive.
        for container in CONTAINER_ROLES:
            scoped = self._tightest(container, target)
            if scoped is not None and (best is None or scoped[1] < fewest):
                best, fewest = scoped
        if best is not None:
            return best.nth(target.nth)
        raise PlaywrightError(
            f"no {target.role!r} named {target.name!r} in any container near {target.near!r}"
        )

    def _tightest(self, container: str, target: ActionTarget) -> tuple[Locator, int] | None:
        """The smallest container of this role holding `near`, with how many nodes it holds.

        Legacy pages lay out with nested tables, so the page chrome is itself a row that
        contains every anchor on the screen. Taking any matching container would resolve
        every `near` to the same first control - so the winner is the one with the fewest
        matching descendants, which is the innermost. The count comes back so the caller can
        compare across container roles too.
        """
        candidates = self._page.get_by_role(cast(Any, container)).filter(has_text=target.near)
        best: tuple[Locator, int] | None = None
        for index in range(candidates.count()):
            locator = self._by_role(candidates.nth(index), target)
            found = locator.count()
            if found and (best is None or found < best[1]):
                best = (locator, found)
        return best

    def _by_role(self, scope: Page | Locator, target: ActionTarget) -> Locator:
        """Role plus name against a page or a narrowed container - the same two facts."""
        role = cast(Any, target.role)
        if target.name:
            return scope.get_by_role(role, name=target.name, exact=target.exact)
        return scope.get_by_role(role)


def _convert(node: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> AXNode:
    """One CDP AX node into an AXNode. Ignored nodes become presentation, so pruning hoists them."""
    raw = "none" if node.get("ignored") else _field(node, "role") or "none"
    role = ROLE_ALIASES.get(raw, raw)
    return AXNode(
        role=role,
        name=_field(node, "name"),
        value=_field(node, "value"),
        children=[
            _convert(by_id[child], by_id) for child in node.get("childIds") or [] if child in by_id
        ],
    )


def _field(node: dict[str, Any], key: str) -> str | None:
    """CDP wraps every value as {"type": ..., "value": ...}."""
    wrapper = node.get(key)
    if not isinstance(wrapper, dict):
        return None
    raw = wrapper.get("value")
    return str(raw) if raw not in (None, "") else None
