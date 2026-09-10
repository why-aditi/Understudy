"""Tenant overlay resolution: load base, apply overrides by path, validate, replay.

Copying a capability per tenant works exactly once. The second time the base changes, every
copy rots silently and nobody finds out until a replay fails in production. An overlay states
only what differs, so the base stays the single description of the flow and a tenant is a
diff against it.

The staleness rule is the part that earns its keep. An overlay records the base version it
was verified against; when the base moves past that, the overlay is *not* silently reapplied.
It is flagged and the resolved capability is demoted to `draft`, which the existing
`replayable_unattended` gate already refuses. One gate for "may this run unattended", not two.

PRD 5.8 exactly: load base -> apply overrides by JSON path -> validate -> replay.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cua.schema.models import Capability, CapabilityOverlay, Override

_log = logging.getLogger(__name__)

OVERLAY_DIR = Path("capabilities/overlays")

# `Override.step_id` names a step. This reserved value scopes an override to the capability
# itself instead, for the fields that are not inside any step - the entry url above all.
# It mirrors the "<precondition>" pseudo-step id the engine already reports failures against.
CAPABILITY_SCOPE = "<capability>"


class OverlayError(Exception):
    """The overlay could not be resolved against this base. Never a silent fallback."""


@dataclass(frozen=True)
class Resolved:
    """A base capability specialised for one tenant, and whether it can be trusted."""

    capability: Capability
    needs_review: bool
    reason: str | None = None


# ---- applying an override by path -----------------------------------------------------------


def _descend(cursor: Any, part: str, where: str) -> Any:  # noqa: ANN401 - walks raw JSON
    """One path segment into a plain dict/list, loudly.

    An override that silently applies to nothing is worse than one that fails: it leaves an
    overlay that looks maintained and changes nothing.
    """
    if isinstance(cursor, list):
        try:
            index = int(part)
        except ValueError:
            raise OverlayError(f"{where}: {part!r} is not a list index") from None
        if not -len(cursor) <= index < len(cursor):
            raise OverlayError(f"{where}: index {index} is out of range ({len(cursor)} items)")
        return cursor[index]
    if isinstance(cursor, dict):
        if part not in cursor:
            raise OverlayError(f"{where}: no field {part!r} (have {sorted(cursor)})")
        return cursor[part]
    raise OverlayError(f"{where}: cannot descend into {type(cursor).__name__} at {part!r}")


def set_path(root: dict[str, Any], path: str, value: Any, *, where: str) -> None:  # noqa: ANN401
    """Replace one field, addressed by a dotted path with integers for list indices."""
    parts = path.split(".")
    if not parts or not all(parts):
        raise OverlayError(f"{where}: {path!r} is not a usable field path")

    cursor: Any = root
    for depth, part in enumerate(parts[:-1]):
        cursor = _descend(cursor, part, f"{where} at {'.'.join(parts[: depth + 1])}")

    leaf = parts[-1]
    # Descend into the leaf first purely to raise the same clear error if it is absent.
    _descend(cursor, leaf, f"{where} at {path}")
    if isinstance(cursor, list):
        cursor[int(leaf)] = value
    else:
        cursor[leaf] = value


def apply_overrides(data: dict[str, Any], overrides: list[Override]) -> None:
    """Every override, in order, onto the base's plain-JSON form. Mutates `data`."""
    steps = {step["id"]: step for step in data["steps"]}
    for override in overrides:
        where = f"override {override.step_id}/{override.field_path}"
        if override.step_id == CAPABILITY_SCOPE:
            set_path(data, override.field_path, override.value, where=where)
            continue
        if override.step_id not in steps:
            raise OverlayError(f"{where}: no step {override.step_id!r} (have {sorted(steps)})")
        set_path(steps[override.step_id], override.field_path, override.value, where=where)


# ---- resolution -------------------------------------------------------------------------------


def resolve(base: Capability, overlay: CapabilityOverlay) -> Resolved:
    """Specialise `base` for the overlay's tenant. The only way a tenant capability is made."""
    if overlay.base_capability_id != base.id:
        raise OverlayError(
            f"overlay targets {overlay.base_capability_id!r}, but the base is {base.id!r}"
        )

    data = base.model_dump(mode="json")
    apply_overrides(data, overlay.overrides)

    data["app"]["tenant_id"] = overlay.tenant_id
    # Recorded so the resolved artifact says out loud that it is a specialisation, not an
    # independently recorded capability that happens to share an id.
    data["app"]["base_capability_id"] = base.id
    data["outcomes"] = data["outcomes"] + [
        outcome.model_dump(mode="json") for outcome in overlay.added_outcomes
    ]

    needs_review = overlay.needs_review(base.version)
    reason = None
    if needs_review:
        reason = (
            f"base {base.id} is at {base.version}, overlay verified against "
            f"{overlay.verified_against}"
        )
        # Demoted rather than refused here: the resolved capability is still perfectly
        # useful to run attended, and `replayable_unattended` is already the one gate that
        # decides. Adding a second refusal path would be a second chokepoint.
        data["provenance"]["state"] = "draft"
        _log.info(
            "overlay_needs_review",
            extra={
                "capability_id": base.id,
                "tenant": overlay.tenant_id,
                "base_version": base.version,
                "verified_against": overlay.verified_against,
            },
        )

    try:
        capability = Capability.model_validate(data)
    except ValidationError as exc:
        raise OverlayError(
            f"overlay for {overlay.tenant_id!r} produced an invalid capability: {exc}"
        ) from exc

    return Resolved(capability=capability, needs_review=needs_review, reason=reason)


# ---- on disk ------------------------------------------------------------------------------------


def overlay_path(capability_id: str, tenant_id: str, directory: Path = OVERLAY_DIR) -> Path:
    return directory / f"{capability_id}.{tenant_id}.json"


def load_overlay(
    capability_id: str, tenant_id: str, directory: Path = OVERLAY_DIR
) -> CapabilityOverlay:
    """Read the overlay specialising `capability_id` for `tenant_id`."""
    path = overlay_path(capability_id, tenant_id, directory)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OverlayError(
            f"no overlay for {capability_id!r} on tenant {tenant_id!r} at {path}: {exc}"
        ) from exc
    try:
        return CapabilityOverlay.model_validate(json.loads(raw))
    except (ValidationError, ValueError) as exc:
        raise OverlayError(f"overlay at {path} is not a valid overlay: {exc}") from exc


def for_tenant(base: Capability, tenant_id: str, directory: Path = OVERLAY_DIR) -> Resolved:
    """Load and apply the tenant's overlay, or pass the base through when it is already theirs."""
    if tenant_id == base.app.tenant_id:
        return Resolved(capability=base, needs_review=False)
    return resolve(base, load_overlay(base.id, tenant_id, directory))
