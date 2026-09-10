"""Structural guards for the architectural constraints. A violation here is a bug, not a nit."""

import ast
import inspect
import logging
import subprocess
import sys
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


def _forwards_its_own_action(
    call: ast.Call, func: ast.FunctionDef | ast.AsyncFunctionDef | None
) -> bool:
    """A Surface decorator handing on the exact action it was given, unchanged.

    `RecordingSurface.act(action)` calls `self._inner.act(action)`. No new action exists:
    the one being forwarded was gated by whoever called the decorator. Requiring a verdict
    here would mean re-checking an action that already passed, which is a second chokepoint
    and therefore the thing C2 forbids.

    Deliberately narrow. The method must be named `act`, the call must pass exactly one
    positional argument, and that argument must be the method's own parameter by name. A
    decorator that builds a different action, or forwards something else, is still flagged.
    """
    if func is None or func.name != "act":
        return False
    parameters = {a.arg for a in [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]}
    if len(call.args) != 1 or call.keywords:
        return False
    argument = call.args[0]
    return isinstance(argument, ast.Name) and argument.id in parameters


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
        if not _is_gated(func) and not _forwards_its_own_action(call, func)
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

# A Surface decorator handing on exactly what it was given: gated by its caller.
FORWARDING_SOURCE = """
class Recording:
    def act(self, action):
        return self._inner.act(action)
"""

# The same shape, but building a different action. Still a bypass.
REWRITING_SOURCE = """
class Rewriting:
    def act(self, action):
        return self._inner.act(Action(kind="click"))
"""

# Forwarding from a method that is not `act` is not the decorator pattern.
SMUGGLING_SOURCE = """
class Smuggler:
    def go(self, action):
        return self._inner.act(action)
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


def test_the_c2_checker_allows_a_decorator_forwarding_its_own_action() -> None:
    """RecordingSurface is this shape. The action it forwards was already gated."""
    assert ungated_act_calls(FORWARDING_SOURCE) == []


def test_the_c2_checker_still_flags_a_decorator_that_rewrites_the_action() -> None:
    """The exemption is for forwarding, not for wrapping. A new action needs a new verdict."""
    assert ungated_act_calls(REWRITING_SOURCE) != []


def test_the_c2_checker_still_flags_forwarding_from_a_method_that_is_not_act() -> None:
    assert ungated_act_calls(SMUGGLING_SOURCE) != []


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


# --- C4: replay makes zero model calls, because no model is reachable from it ------------

FORBIDDEN_FOR_REPLAY = "cua.llm"


def _cua_imports(path: Path) -> set[str]:
    """Every `cua.*` module this file imports, including imports inside functions."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("cua"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("cua"):
            found.add(node.module)
            # `from cua.replay import engine` names the submodule in the alias, not the module.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _module_path(module: str) -> Path | None:
    base = SRC.parents[0] / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def import_closure(module: str) -> set[str]:
    """Every cua module transitively reachable from this one."""
    seen: set[str] = set()
    queue = [module]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        path = _module_path(current)
        if path is not None:
            queue.extend(_cua_imports(path) - seen)
    return seen


def test_c4_no_llm_module_is_reachable_from_the_replay_engine() -> None:
    """C4: replay makes zero model calls. Enforced by what it is allowed to import."""
    closure = import_closure("cua.replay.engine")
    offenders = sorted(m for m in closure if m.startswith(FORBIDDEN_FOR_REPLAY))
    assert not offenders, f"cua.replay.engine can reach {offenders}"
    assert "cua.replay.resolver" in closure, "the closure walk found nothing; check the paths"


def test_the_c4_checker_would_notice_an_llm_import() -> None:
    """A constraint check that cannot fail is not a check."""
    closure = import_closure("cua.discovery.runner")
    assert any(m.startswith(FORBIDDEN_FOR_REPLAY) for m in closure), (
        "the discovery runner does use an LLM, so the checker must see it there"
    )


def test_c4_holds_at_runtime_not_just_on_paper() -> None:
    """Import the engine in a clean interpreter; no llm module may end up loaded."""
    probe = (
        "import sys; import cua.replay.engine; "
        "print([m for m in sys.modules if m.startswith('cua.llm')])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=SRC.parents[1],
        check=True,
    )
    assert completed.stdout.strip() == "[]", f"llm modules loaded: {completed.stdout.strip()}"


def test_the_replay_engine_takes_no_llm_collaborator() -> None:
    """The constructor is the seam an LLM would have to arrive through. It has no such door."""
    from cua.replay.engine import ReplayEngine

    parameters = set(inspect.signature(ReplayEngine.__init__).parameters)
    assert parameters == {
        "self",
        "surface",
        "policy",
        "logger",
        "resolver",
        "allow_screenshots",
        "monotonic",
        "sleep",
    }


# --- C1: the session outlives the run, so a run may not own a browser -------------------

# Launching or closing a browser is ownership. A run that does either cannot hand the same
# live window to a human, which is the entire requirement.
BROWSER_OWNERSHIP = ("sync_playwright", "chromium.launch", "close_all", ".open(")

RUN_MODULES = ("discovery", "replay", "recording")


def _owns_a_browser(source: str) -> list[str]:
    return [marker for marker in BROWSER_OWNERSHIP if marker in source]


def test_c1_no_run_module_creates_or_closes_a_browser() -> None:
    """C1: runs attach to a session held by the registry; they never own one."""
    offenders = []
    for package in RUN_MODULES:
        for path in sorted((SRC / package).rglob("*.py")):
            found = _owns_a_browser(path.read_text(encoding="utf-8"))
            if found:
                offenders.append(f"{path.relative_to(SRC.parents[1])} uses {found}")
    assert not offenders, "; ".join(offenders)


def test_only_the_registry_launches_browsers() -> None:
    """One place creates browsers, so there is one place that can hand one over."""
    launchers = [
        path.relative_to(SRC.parents[1]).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if "sync_playwright" in path.read_text(encoding="utf-8")
    ]
    assert launchers == ["src/cua/session/registry.py"], launchers


def test_the_c1_checker_would_notice_a_run_launching_its_own_browser() -> None:
    assert _owns_a_browser("with sync_playwright() as p: ...") == ["sync_playwright"]
    assert _owns_a_browser("surface.act(action)") == []
