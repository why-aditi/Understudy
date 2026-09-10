"""Closed action tool schema the discovery model is allowed to emit."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from cua.llm.base import ToolCall, ToolSpec
from cua.surfaces.base import Action, ActionTarget

# Gemini's function-declaration schema accepts a strict OpenAPI subset. These keys are
# valid JSON Schema but are rejected there, so they never leave this module.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({"title", "default"})


class ClosedSchemaViolation(Exception):
    """The model emitted something outside the tool schema.

    This is a hard error and never a retry: a loop that negotiates with a model about
    which actions exist has no closed schema, and the policy chokepoint downstream can
    only gate actions it recognises.
    """


class Finish(BaseModel):
    """Not an action. The model's claim that the goal is met, plus what it found."""

    summary: str
    outputs: dict[str, str] = Field(default_factory=dict)


class _Target(BaseModel):
    role: str = Field(description="Accessibility role, e.g. button, link, textbox, cell.")
    name: str | None = Field(
        default=None, description="Accessible name exactly as shown in the tree."
    )
    near: str | None = Field(
        default=None,
        description=(
            "Text of a nearby element that identifies which one you mean, when several "
            'controls share a role and name. To open the Savings row, use near="Savings" '
            "rather than counting rows. Prefer this over nth."
        ),
    )
    nth: int = Field(
        default=0,
        description=(
            "ZERO-BASED index among the controls that match, used only when near cannot "
            "disambiguate. The first match is nth=0, the second is nth=1."
        ),
    )

    def to_target(self) -> ActionTarget:
        """Exact by default, because the field above promises the name as shown in the tree.

        Substring matching is a silent footgun on a page with overlapping labels: on the
        harness search screen a substring click on "Search" resolves to the "Member search"
        nav link, which points at the same page. The action reports ok, the url does not
        change, and the loop spends its remaining turns confused. Recorded locators already
        carry match=exact, which is why replay never hit this and discovery did.
        """
        return ActionTarget(
            role=self.role,
            name=self.name or None,
            nth=self.nth,
            near=self.near or None,
            exact=True,
        )


class _Navigate(BaseModel):
    url: str = Field(description="Absolute url to open.")


class _Click(_Target):
    pass


class _TypeText(_Target):
    text: str = Field(description="Text to type into the control.")


class _SelectOption(_Target):
    option: str = Field(description="Option label or value to select.")


class _PressKey(BaseModel):
    key: str = Field(description="Key to press, e.g. Enter or Escape.")


class _WaitFor(_Target):
    pass


class _Extract(_Target):
    pass


class _Finish(BaseModel):
    summary: str = Field(description="One sentence on how the goal was accomplished.")
    outputs: dict[str, str] = Field(
        default_factory=dict, description="Values read from the page that answer the goal."
    )


def _clean(node: object) -> object:
    """Strip JSON Schema keys the provider rejects."""
    if isinstance(node, dict):
        return _clean_schema(node)
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


def _collapse_optional(schema: dict[str, Any]) -> dict[str, Any]:
    """`anyOf: [{string}, {null}]` becomes `type: [string, null]`.

    An optional field has to be expressible as *absent or null*, because a model asked for
    "no name" will emit null. Providers validate the emitted arguments against the schema we
    sent them, and at least one rejects `anyOf` while accepting a type list - so a schema that
    says `type: string` for an optional field turns a reasonable model output into an HTTP 400
    the loop never sees coming. Found exactly that way.
    """
    variants = schema.get("anyOf")
    if not isinstance(variants, list) or not all(
        isinstance(v, dict) and set(v) <= {"type"} and isinstance(v.get("type"), str)
        for v in variants
    ):
        return schema
    rest = {k: v for k, v in schema.items() if k != "anyOf"}
    return {**rest, "type": [v["type"] for v in variants]}


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    cleaned = {k: _clean(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    return _collapse_optional(cleaned)


def _spec(name: str, description: str, model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(
        name=name, description=description, parameters=_clean_schema(model.model_json_schema())
    )


# name -> (argument model, how that model becomes an Action). The dict is the schema:
# a tool that is not in here does not exist, and the model cannot argue otherwise.
_TOOLS: dict[str, tuple[type[BaseModel], Callable[[Any], Action | Finish]]] = {
    "navigate": (_Navigate, lambda a: Action(kind="navigate", value=a.url)),
    "click": (_Click, lambda a: Action(kind="click", target=a.to_target())),
    "type_text": (_TypeText, lambda a: Action(kind="type", target=a.to_target(), value=a.text)),
    "select_option": (
        _SelectOption,
        lambda a: Action(kind="select", target=a.to_target(), value=a.option),
    ),
    "press_key": (_PressKey, lambda a: Action(kind="press_key", value=a.key)),
    "wait_for": (_WaitFor, lambda a: Action(kind="wait_for", target=a.to_target())),
    "extract": (_Extract, lambda a: Action(kind="extract", target=a.to_target())),
    "finish": (_Finish, lambda a: Finish(summary=a.summary, outputs=a.outputs)),
}

_DESCRIPTIONS: dict[str, str] = {
    "navigate": "Open a url.",
    "click": "Click a control.",
    "type_text": "Type text into a control.",
    "select_option": "Choose an option in a combobox or list.",
    "press_key": "Press a keyboard key.",
    "wait_for": "Wait until a control is visible.",
    "extract": (
        "Read the text of a control. To read a value that sits beside a label, anchor on "
        'the label and take the next cell: near="Current balance", nth=1. nth=0 is the '
        "label's own cell, not the value."
    ),
    "finish": "Declare the goal accomplished and report what was found.",
}

TOOL_SPECS: tuple[ToolSpec, ...] = tuple(
    _spec(name, _DESCRIPTIONS[name], model) for name, (model, _) in _TOOLS.items()
)

TOOL_NAMES = frozenset(_TOOLS)


def parse(call: ToolCall) -> Action | Finish:
    """Turn one tool call into an Action, or into Finish. Anything else raises."""
    entry = _TOOLS.get(call.name)
    if entry is None:
        raise ClosedSchemaViolation(
            f"model called unknown tool {call.name!r}; the schema is {sorted(TOOL_NAMES)}"
        )
    model, build = entry
    try:
        args = model.model_validate(call.arguments)
    except ValidationError as exc:
        raise ClosedSchemaViolation(
            f"model called {call.name!r} with arguments outside the schema: {exc}"
        ) from exc
    return build(args)


def one_call(calls: list[ToolCall]) -> ToolCall:
    """Exactly one action per turn. Zero or several is a hard error, not a retry."""
    if len(calls) != 1:
        raise ClosedSchemaViolation(
            f"expected exactly one tool call per turn, model emitted {len(calls)}"
        )
    return calls[0]
