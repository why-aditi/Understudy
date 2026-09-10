"""WebSurface reads the AX tree and acts through role plus name only - never a selector."""

from typing import Any, cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from cua.surfaces.base import Action, ActionTarget, Surface
from cua.surfaces.web import WebSurface

# A CDP Accessibility.getFullAXTree payload: wrapper nodes, an ignored node, real controls.
AX_PAYLOAD: dict[str, Any] = {
    "nodes": [
        {
            "nodeId": "1",
            "role": {"value": "RootWebArea"},
            "name": {"value": "Members"},
            "childIds": ["2"],
        },
        {"nodeId": "2", "role": {"value": "generic"}, "childIds": ["3", "4"]},
        {"nodeId": "3", "role": {"value": "button"}, "name": {"value": "Search"}},
        {"nodeId": "4", "ignored": True, "role": {"value": "paragraph"}, "childIds": ["5"]},
        {"nodeId": "5", "role": {"value": "StaticText"}, "name": {"value": "Balance"}},
    ]
}


class FakeLocator:
    def __init__(self, page: "FakePage", raises: PlaywrightError | None) -> None:
        self._page = page
        self._raises = raises

    def nth(self, index: int) -> "FakeLocator":
        self._page.calls.append(("nth", index))
        return self

    def _record(self, name: str, *args: object) -> None:
        self._page.calls.append((name, *args))
        if self._raises is not None:
            raise self._raises

    def click(self, **kwargs: object) -> None:
        self._record("click")

    def fill(self, value: str, **kwargs: object) -> None:
        self._record("fill", value)

    def inner_text(self, **kwargs: object) -> str:
        self._record("inner_text")
        return "  1,234.56  "


class FakeCDP:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.sent: list[str] = []

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append(method)
        return self.payload if method == "Accessibility.getFullAXTree" else {}


class FakeContext:
    def __init__(self, cdp: FakeCDP) -> None:
        self._cdp = cdp

    def new_cdp_session(self, page: object) -> FakeCDP:
        return self._cdp


class FakePage:
    """Only the handful of Page members WebSurface is allowed to touch."""

    def __init__(self, payload: dict[str, Any] = AX_PAYLOAD) -> None:
        self.url = "http://localhost:8080/members"
        self.cdp = FakeCDP(payload)
        self.context = FakeContext(self.cdp)
        self.calls: list[tuple[object, ...]] = []
        self.raises: PlaywrightError | None = None

    def title(self) -> str:
        return "Members"

    def screenshot(self, **kwargs: object) -> bytes:
        self.calls.append(("screenshot",))
        return b"\x89PNG"

    def get_by_role(self, role: str, **kwargs: object) -> FakeLocator:
        self.calls.append(("get_by_role", role, kwargs.get("name"), kwargs.get("exact")))
        return FakeLocator(self, self.raises)

    def goto(self, url: str, **kwargs: object) -> None:
        self.calls.append(("goto", url))


def surface(page: FakePage) -> WebSurface:
    return WebSurface(cast(Page, page))


def test_web_surface_satisfies_the_protocol() -> None:
    page = FakePage()
    typed: Surface = surface(page)
    assert isinstance(typed, Surface)


def test_observe_prunes_the_cdp_tree() -> None:
    page = FakePage()

    observation = surface(page).observe()

    assert observation.url == page.url
    assert observation.title == "Members"
    assert observation.tree is not None
    # generic wrapper and ignored paragraph are spliced; their content survives.
    assert [(c.role, c.name) for c in observation.tree.children] == [
        ("button", "Search"),
        ("StaticText", "Balance"),
    ]
    assert (observation.pruning.nodes_before, observation.pruning.nodes_after) == (5, 3)
    assert page.cdp.sent[0] == "Accessibility.enable"


def test_screenshots_are_off_by_default() -> None:
    page = FakePage()
    assert surface(page).observe().screenshot is None
    assert ("screenshot",) not in page.calls

    assert surface(page).observe(screenshot=True).screenshot == b"\x89PNG"


def test_screenshot_bytes_never_reach_a_dump() -> None:
    """Evidence records the AX tree, not the pixels."""
    observation = surface(FakePage()).observe(screenshot=True)
    assert "screenshot" not in observation.model_dump()


def test_observation_hash_is_carried_and_stable() -> None:
    first = surface(FakePage()).observe()
    second = surface(FakePage()).observe()
    assert first.observation_hash == second.observation_hash


def test_click_goes_through_role_and_name() -> None:
    page = FakePage()
    action = Action(kind="click", target=ActionTarget(role="button", name="Search", nth=2))

    result = surface(page).act(action)

    assert result.ok and result.error is None
    assert page.calls == [("get_by_role", "button", "Search", False), ("nth", 2), ("click",)]


def test_extract_returns_stripped_text() -> None:
    page = FakePage()
    action = Action(kind="extract", target=ActionTarget(role="cell", name="Balance"))

    result = surface(page).act(action)

    assert result.extracted == "  1,234.56  "
    assert result.ok


def test_navigate_needs_no_target() -> None:
    page = FakePage()
    result = surface(page).act(Action(kind="navigate", value="http://localhost:8080/"))
    assert result.ok
    assert ("goto", "http://localhost:8080/") in page.calls


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (Action(kind="click"), "click requires a target"),
        (Action(kind="navigate"), "navigate requires a value"),
    ],
)
def test_malformed_actions_come_back_as_failures_not_exceptions(
    action: Action, message: str
) -> None:
    page = FakePage()
    result = surface(page).act(action)
    assert not result.ok
    assert result.error == message
    assert page.calls == []


def test_a_missing_control_is_a_failed_result_not_a_raise() -> None:
    page = FakePage()
    page.raises = PlaywrightError("Timeout 10000ms exceeded.\nwaiting for get_by_role")

    result = surface(page).act(
        Action(kind="click", target=ActionTarget(role="button", name="Nope"))
    )

    assert not result.ok
    assert result.error is not None and "Timeout 10000ms exceeded." in result.error
    assert result.url_after == page.url


# --- near=: pick the tightest container, not merely a matching one ---------------------


class ScopedLocator:
    """A locator that knows how many controls it holds, and which one it hands back."""

    def __init__(self, label: str, matches: list[str], recorder: list[str]) -> None:
        self.label = label
        self.matches = matches
        self._recorder = recorder

    def count(self) -> int:
        return len(self.matches)

    def nth(self, index: int) -> "ScopedLocator":
        return ScopedLocator(self.label, self.matches[index : index + 1], self._recorder)

    def click(self, **kwargs: object) -> None:
        self._recorder.append(self.matches[0])


class Containers:
    """The set of containers of one role, filterable by the text they contain."""

    def __init__(self, scopes: dict[str, list[str]], recorder: list[str]) -> None:
        self.scopes = scopes
        self._recorder = recorder
        self._selected: list[str] = []

    def filter(self, has_text: str) -> "Containers":
        clone = Containers(self.scopes, self._recorder)
        clone._selected = [name for name, text in CONTAINER_TEXT.items() if has_text in text]
        return clone

    def count(self) -> int:
        return len(self._selected)

    def nth(self, index: int) -> "Scope":
        return Scope(self._selected[index], self.scopes, self._recorder)


class Scope:
    def __init__(self, label: str, scopes: dict[str, list[str]], recorder: list[str]) -> None:
        self.label = label
        self._scopes = scopes
        self._recorder = recorder

    def get_by_role(self, role: str, **kwargs: object) -> ScopedLocator:
        return ScopedLocator(self.label, self._scopes.get(self.label, []), self._recorder)


# The harness shape: the page chrome is itself a row containing every account name,
# and each account has its own row holding exactly one "Open" link.
CONTAINER_TEXT = {
    "page-chrome-row": "Savings Chequing Term deposit",
    "savings-row": "Savings",
    "chequing-row": "Chequing",
}
SCOPE_LINKS = {
    "page-chrome-row": ["open-savings", "open-chequing", "open-term"],
    "savings-row": ["open-savings"],
    "chequing-row": ["open-chequing"],
}


class NearPage:
    """Only what act() touches: a url and a container-aware get_by_role."""

    def __init__(self) -> None:
        self.url = "http://localhost:8099/tenant-a/members/12345"
        self.clicked: list[str] = []

    def get_by_role(self, role: str, **kwargs: object) -> Containers:
        return Containers(SCOPE_LINKS, self.clicked)


def near_surface(page: NearPage) -> WebSurface:
    return WebSurface(cast(Page, page))


def test_near_prefers_the_tightest_container() -> None:
    """The page chrome also contains 'Chequing'; the chequing row is the smaller match."""
    page = NearPage()
    action = Action(kind="click", target=ActionTarget(role="link", name="Open", near="Chequing"))

    result = near_surface(page).act(action)

    assert result.ok
    assert page.clicked == ["open-chequing"]


def test_near_that_matches_nothing_is_a_failed_result() -> None:
    page = NearPage()
    action = Action(kind="click", target=ActionTarget(role="link", name="Open", near="Mortgage"))

    result = near_surface(page).act(action)

    assert not result.ok
    assert result.error is not None and "Mortgage" in result.error
