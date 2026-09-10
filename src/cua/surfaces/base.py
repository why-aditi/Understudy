"""Surface protocol: the observe/act contract every surface implements."""

from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

ActionKind = Literal["navigate", "click", "type", "select", "press_key", "wait_for", "extract"]


class AXNode(BaseModel):
    """One accessibility-tree node, in the vocabulary every surface shares.

    Role, name and value are what a browser AX tree, a Windows UIA tree and an
    NSAccessibility tree all agree on. Nothing here is web-specific (C3).
    """

    role: str
    name: str | None = None
    value: str | None = None
    children: list["AXNode"] = Field(default_factory=list)


class PruningStats(BaseModel):
    """Evidence that observation pruning happened, and how hard it bit (D3)."""

    nodes_before: int
    nodes_after: int
    rows_collapsed: int = 0
    values_truncated: int = 0

    @property
    def ratio(self) -> float:
        """Fraction of nodes removed. 0.0 when nothing was pruned."""
        if self.nodes_before == 0:
            return 0.0
        return 1.0 - (self.nodes_after / self.nodes_before)


class ActionTarget(BaseModel):
    """A control named the way every accessibility API names one: role plus name.

    `replay.resolver` narrows a full ControlDescriptor candidate chain down to one of
    these before it reaches a surface, so no surface ever sees a locator strategy.
    """

    role: str
    name: str | None = None
    nth: int = 0
    exact: bool = False
    # Text of a nearby element that says *which* one, when role and name repeat: the
    # "Open" link in the row that says "Savings". Every accessibility tree has containers,
    # so this resolves against a desktop AX tree with the same two facts (C3).
    near: str | None = None


class Action(BaseModel):
    """A single thing to do to a surface. The only vocabulary the policy engine gates."""

    kind: ActionKind
    target: ActionTarget | None = None
    value: str | None = None  # url to navigate to, text to type, key to press, option to select
    timeout_ms: int = 10_000


class Observation(BaseModel):
    """What a surface looks like right now: the AX tree first, a screenshot only if asked."""

    url: str
    title: str | None = None
    tree: AXNode | None = None
    observation_hash: str
    pruning: PruningStats
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Excluded from every dump: evidence records the AX tree, never the pixels.
    screenshot: bytes | None = Field(default=None, exclude=True, repr=False)


class ActionResult(BaseModel):
    """The outcome of one action, including the failure detail a replay step needs."""

    action: Action
    ok: bool
    url_after: str
    duration_ms: int
    extracted: str | None = None
    error: str | None = None


@runtime_checkable
class Surface(Protocol):
    """Every surface - web today, desktop later - implements exactly this."""

    def observe(self, *, screenshot: bool = False) -> Observation:
        """Capture the current state. Screenshots are opt-in and default to off."""
        ...

    def act(self, action: Action) -> ActionResult:
        """Perform one action. Never raises for an expected failure; returns ok=False."""
        ...
