"""Structural guards for the architectural constraints. A violation here is a bug, not a nit."""

import ast
import logging
from pathlib import Path

import pytest

SURFACES = Path(__file__).resolve().parents[1] / "src" / "cua" / "surfaces"

# Playwright APIs that reach past the accessibility tree into the DOM.
DOM_SELECTOR_APIS = ("query_selector", ".locator(", "evaluate", "css=", "xpath=")


@pytest.mark.parametrize("path", sorted(SURFACES.glob("*.py")), ids=lambda p: p.name)
def test_c3_surfaces_never_observe_through_a_dom_selector(path: Path) -> None:
    """C3: no surface-specific locator is a primary strategy, so surfaces read AX only."""
    source = path.read_text(encoding="utf-8")
    found = [api for api in DOM_SELECTOR_APIS if api in source]
    assert not found, f"{path.name} reaches into the DOM via {found}"


# --- C2: every action reaches a surface through PolicyEngine.check() ------------------

SRC = Path(__file__).resolve().parents[1] / "src" / "cua"

# Evidence, in the calling function's body, that a verdict was actually obtained.
# A mention in the return annotation does not count: it promises a verdict, it does not get one.
GATE_MARKERS = ("verdict", "PolicyVerdict")


def _act_calls(
    node: ast.AST,
    enclosing: ast.FunctionDef | ast.AsyncFunctionDef | None,
    found: list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]],
) -> None:
    for child in ast.iter_child_nodes(node):
        scope = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else enclosing
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "act"
        ):
            found.append((child, enclosing))
        _act_calls(child, scope, found)


def _receives_a_verdict(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """The verdict was obtained by the caller and handed in as an argument."""
    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if arg.arg in GATE_MARKERS:
            return True
        annotation = arg.annotation
        if annotation is not None and any(
            isinstance(node, ast.Name) and node.id in GATE_MARKERS for node in ast.walk(annotation)
        ):
            return True
    return False


def _is_gated(func: ast.FunctionDef | ast.AsyncFunctionDef | None) -> bool:
    """A call site is gated when its own function obtains or receives a PolicyVerdict.

    Only the body and the parameters count. Scanning the whole function would let a
    return annotation of `-> PolicyVerdict` pass a function that never calls check().
    """
    if func is None:
        return False  # a module-level act() call can never have been gated
    if _receives_a_verdict(func):
        return True
    for statement in func.body:
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "check"
            ):
                return True
            if isinstance(node, ast.Name) and node.id in GATE_MARKERS:
                return True
    return False


def ungated_act_calls(source: str, filename: str = "<test>") -> list[str]:
    """Call sites of Surface.act() whose function never obtained a PolicyVerdict."""
    found: list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]] = []
    _act_calls(ast.parse(source), None, found)
    return [
        f"{filename}:{call.lineno} in {func.name if func else '<module>'}"
        for call, func in found
        if not _is_gated(func)
    ]


GATED_SOURCE = """
def run(surface, engine, action, context):
    verdict = engine.check(action, context)
    if verdict.allowed:
        surface.act(action)
"""

UNGATED_SOURCE = """
def run(surface, action):
    surface.act(action)
"""

MODULE_LEVEL_SOURCE = """
surface.act(action)
"""


def test_the_c2_checker_accepts_a_gated_call() -> None:
    assert ungated_act_calls(GATED_SOURCE) == []


def test_the_c2_checker_flags_an_ungated_call() -> None:
    assert ungated_act_calls(UNGATED_SOURCE) == ["<test>:3 in run"]


def test_the_c2_checker_flags_a_module_level_call() -> None:
    assert ungated_act_calls(MODULE_LEVEL_SOURCE) == ["<test>:2 in <module>"]


def test_c2_no_action_reaches_a_surface_without_a_policy_verdict() -> None:
    """C2: one chokepoint. A call to Surface.act() with no verdict in scope is a bug."""
    sources = sorted(SRC.rglob("*.py"))
    assert sources, "found no source files to scan"

    offenders = [
        finding
        for path in sources
        for finding in ungated_act_calls(
            path.read_text(encoding="utf-8"), str(path.relative_to(SRC.parents[1]))
        )
    ]
    assert not offenders, "Surface.act() called without PolicyEngine.check(): " + "; ".join(
        offenders
    )


ANNOTATION_ONLY_SOURCE = """
def run(surface, action) -> PolicyVerdict:
    return surface.act(action)
"""

RECEIVES_VERDICT_SOURCE = """
def run(surface, action, verdict):
    if verdict.allowed:
        surface.act(action)
"""


def test_the_c2_checker_is_not_fooled_by_a_return_annotation() -> None:
    """Promising a PolicyVerdict is not the same as obtaining one."""
    assert ungated_act_calls(ANNOTATION_ONLY_SOURCE) == ["<test>:3 in run"]


def test_the_c2_checker_accepts_a_verdict_passed_in_by_the_caller() -> None:
    assert ungated_act_calls(RECEIVES_VERDICT_SOURCE) == []


# --- structured logging: `extra` must not shadow LogRecord's own attributes -------------

# logging raises KeyError if `extra` carries any of these, but only once a handler has the
# logger at INFO. It is therefore invisible in unit tests and fatal in a real run.
RESERVED_LOG_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}


def _log_field_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Every key passed as a structured log field, with its line number.

    Two shapes carry them: `logger.info(event, extra={...})` and `RunLogger.event(name, k=v)`.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                found.extend(
                    (key.lineno, key.value)
                    for key in keyword.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
            elif keyword.arg and isinstance(node.func, ast.Attribute) and node.func.attr == "event":
                found.append((node.lineno, keyword.arg))
    return found


def test_no_log_field_shadows_a_logrecord_attribute() -> None:
    """`extra={"name": ...}` raises KeyError the moment the logger is at INFO."""
    offenders = [
        f"{path.relative_to(SRC.parents[1])}:{line} uses reserved log field {field!r}"
        for path in sorted(SRC.rglob("*.py"))
        for line, field in _log_field_names(ast.parse(path.read_text(encoding="utf-8")))
        if field in RESERVED_LOG_KEYS
    ]
    assert not offenders, "; ".join(offenders)


def test_the_reserved_log_key_checker_catches_a_real_collision() -> None:
    source = '_log.info("evt", extra={"name": x, "control_name": y})'
    fields = [field for _, field in _log_field_names(ast.parse(source))]
    assert "name" in fields and "control_name" in fields
    assert "name" in RESERVED_LOG_KEYS
    assert "control_name" not in RESERVED_LOG_KEYS
