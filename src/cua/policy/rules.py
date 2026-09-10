"""Allowlist and risk-classification table mapping an action to safe, risky, or irreversible."""

import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError

from cua.surfaces.base import Action, ActionKind

RiskClass = Literal["safe", "risky", "irreversible"]

DEFAULT_POLICY_PATH = Path("policy.yaml")

# Reading a page never changes it, whatever the route is called.
READ_ONLY_KINDS: frozenset[ActionKind] = frozenset({"extract", "wait_for"})


class PolicyConfigError(Exception):
    """policy.yaml is missing or malformed. Safety config fails closed, never open."""


class Allowlist(BaseModel):
    """Where the system may go and what it may do once there."""

    hosts: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=lambda: ["/*"])
    actions: list[ActionKind] = Field(default_factory=list)


class Limits(BaseModel):
    """Run ceilings enforced by the discovery and replay runners."""

    max_steps: int = 25
    max_wall_clock_seconds: int = 300


class RiskKeywords(BaseModel):
    """Words that move an action up the risk table when they appear in a target or route."""

    risky: list[str] = Field(default_factory=lambda: ["submit", "save", "create"])
    irreversible: list[str] = Field(
        default_factory=lambda: ["delete", "transfer", "close", "disburse", "wire"]
    )


class Policy(BaseModel):
    """The whole safety configuration, global or per capability."""

    allowlist: Allowlist = Field(default_factory=Allowlist)
    limits: Limits = Field(default_factory=Limits)
    risk: RiskKeywords = Field(default_factory=RiskKeywords)


def load_policy(path: Path | None = None) -> Policy:
    """Load policy.yaml. A missing or broken file is an error, not a permissive default."""
    path = path or DEFAULT_POLICY_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyConfigError(f"cannot read policy at {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise PolicyConfigError(f"{path} must contain a mapping, got {type(parsed).__name__}")

    try:
        return Policy.model_validate(parsed)
    except ValidationError as exc:
        raise PolicyConfigError(f"{path} is not a valid policy: {exc}") from exc


def action_url(action: Action, current_url: str | None) -> str | None:
    """The url an action is judged against: where it goes, or where it happens."""
    if action.kind == "navigate":
        return action.value
    return current_url


def host_allowed(url: str, allowlist: Allowlist) -> bool:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    hostname = (parsed.hostname or "").lower()
    if not netloc:
        return False
    return any(
        fnmatchcase(netloc, pattern.lower()) or fnmatchcase(hostname, pattern.lower())
        for pattern in allowlist.hosts
    )


def route_allowed(url: str, allowlist: Allowlist) -> bool:
    path = urlparse(url).path or "/"
    return any(fnmatchcase(path.lower(), pattern.lower()) for pattern in allowlist.routes)


def _mentions(haystack: str, keywords: list[str]) -> str | None:
    """Whole-word keyword match. Returns the word that fired, for the verdict reason."""
    for word in keywords:
        if re.search(rf"\b{re.escape(word)}\b", haystack, re.IGNORECASE):
            return word
    return None


def classify(
    action: Action,
    policy: Policy,
    *,
    current_url: str | None = None,
    step_declared_irreversible: bool = False,
) -> tuple[RiskClass, str]:
    """Risk class per TECH-ARCH section 7, plus the reason it landed there.

    The bias is deliberate: a control named "Closed accounts" classifying as irreversible
    costs one escalation, while missing a real "Close account" button costs a real account.
    """
    if action.kind in READ_ONLY_KINDS:
        return "safe", f"{action.kind} does not change state"

    if step_declared_irreversible:
        return "irreversible", "step is declared irreversible in the capability"

    haystack = " ".join(
        part
        for part in (
            action.target.name if action.target else None,
            action.value if action.kind != "type" else None,
            urlparse(action_url(action, current_url) or "").path or None,
        )
        if part
    )

    fired = _mentions(haystack, policy.risk.irreversible)
    if fired:
        return "irreversible", f"target or route matches irreversible keyword {fired!r}"

    if action.kind == "click":
        fired = _mentions(haystack, policy.risk.risky)
        if fired:
            return "risky", f"click on a control matching risky keyword {fired!r}"

    return "safe", f"{action.kind} is not a state-changing control"
