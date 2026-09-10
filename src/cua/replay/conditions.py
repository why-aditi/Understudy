"""Evaluates Conditions used as step checkpoints and outcome detectors.

There is one evaluator, deliberately. A checkpoint asks "did that step work?" and a detector
asks "is this a situation I know about?", but both are the same question about the same
screen, and two implementations would drift until a checkpoint passed where the matching
detector did not fire - which is precisely the state the error taxonomy exists to prevent.
"""

import logging
import re
from typing import Any

from cua.replay.resolver import matches, text_of, walk
from cua.schema.models import Condition, Locator, Outcome
from cua.surfaces.base import AXNode

_log = logging.getLogger(__name__)


class ConditionError(Exception):
    """The condition is not evaluable: an unknown kind, or a missing parameter."""


def _control_locator(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": params.get("role", ""),
        "name": params.get("name", ""),
        "match": params.get("match", "exact"),
    }


def _controls(tree: AXNode | None, params: dict[str, Any]) -> int:
    """How many controls match role and name. Reuses the resolver's role_name semantics."""
    if tree is None:
        return 0
    probe = Locator(
        strategy="role_name",
        params=_control_locator(params),
        stability_score=0.0,
        verified_unique_at_record=False,
    )
    return len(matches(tree, probe))


def _text_present(tree: AXNode | None, params: dict[str, Any]) -> bool:
    needle = str(params.get("text", "")).strip().lower()
    if not needle:
        raise ConditionError("text_present needs a 'text' parameter")
    if tree is None:
        return False
    return any(needle in text_of(node).lower() for node in walk(tree))


def _url_matches(url: str, params: dict[str, Any]) -> bool:
    pattern = str(params.get("pattern", ""))
    if not pattern:
        raise ConditionError("url_matches needs a 'pattern' parameter")
    try:
        return re.search(pattern, url) is not None
    except re.error as exc:
        raise ConditionError(
            f"url_matches pattern {pattern!r} is not a valid regex: {exc}"
        ) from exc


def _value_equals(tree: AXNode | None, params: dict[str, Any]) -> bool:
    """Whether the named control holds the expected value.

    Reads the control's own value, falling back to its accessible name, because a legacy
    surface presents a read-only field as text about as often as it does as an input.
    """
    if tree is None:
        return False
    expected = str(params.get("value", ""))
    probe = Locator(
        strategy="role_name",
        params=_control_locator(params),
        stability_score=0.0,
        verified_unique_at_record=False,
    )
    found = matches(tree, probe)
    return any((node.value if node.value is not None else node.name) == expected for node in found)


def evaluate(condition: Condition, tree: AXNode | None, url: str = "") -> bool:
    """Whether this condition holds on the given screen.

    The one evaluator: `replay.engine` calls it for a step's checkpoint and for every
    outcome detector, so the two can never disagree about what the screen says.
    """
    kind, params = condition.kind, condition.params

    if kind == "control_present":
        result = _controls(tree, params) > 0
    elif kind == "control_absent":
        result = _controls(tree, params) == 0
    elif kind == "text_present":
        result = _text_present(tree, params)
    elif kind == "url_matches":
        result = _url_matches(url, params)
    elif kind == "value_equals":
        result = _value_equals(tree, params)
    else:  # pragma: no cover - Literal keeps this unreachable
        raise ConditionError(f"unknown condition kind {kind!r}")

    return not result if condition.negate else result


def describe(condition: Condition) -> str:
    """A one-line rendering, for the `expected` field of a failure report."""
    detail = ", ".join(f"{k}={v!r}" for k, v in sorted(condition.params.items()))
    return f"{'not ' if condition.negate else ''}{condition.kind}({detail})"


def first_matching(
    outcomes: list[Outcome], tree: AXNode | None, url: str, step_id: str
) -> Outcome | None:
    """The first declared outcome whose detector fires on this screen.

    Order is the artifact's order, so a capability can put a specific detector ahead of a
    general one. Outcomes scoped to other steps are not considered.
    """
    for outcome in outcomes:
        if outcome.applies_to != "any" and step_id not in outcome.applies_to:
            continue
        if evaluate(outcome.detect, tree, url):
            _log.info(
                "outcome_detected",
                extra={"outcome": outcome.name, "kind": outcome.kind, "step": step_id},
            )
            return outcome
    return None
