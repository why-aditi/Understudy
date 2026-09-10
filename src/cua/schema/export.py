"""JSON Schema export: the agent-facing contract derived from the Pydantic models.

An agent decides whether to call a capability, and how, from this schema alone. That makes
the descriptions part of the contract rather than documentation: a property with no
description is a property the caller has to guess at, so `undescribed_properties` finds them
and a test fails the build when it finds any.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.schema.models import Capability, CapabilityOverlay, ReplayResult

DEFAULT_SCHEMA_DIR = Path("capabilities/schema")

# The models an agent or an integrator reads. Capability is the artifact, ReplayResult is
# what a call returns, CapabilityOverlay is how a tenant differs.
EXPORTED: dict[str, type[BaseModel]] = {
    "capability": Capability,
    "capability-overlay": CapabilityOverlay,
    "replay-result": ReplayResult,
}


def json_schema(model: type[BaseModel] = Capability) -> dict[str, Any]:
    """The JSON Schema for a model, with `$defs` for every nested type."""
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://understudy.local/schema/{model.__name__.lower()}.json"
    return schema


def undescribed_properties(schema: dict[str, Any]) -> list[str]:
    """Paths to properties that carry no description, i.e. holes in the contract.

    A `$ref` needs no description of its own: it points at a definition that has one.
    """
    holes: list[str] = []

    def walk(node: dict[str, Any], path: str) -> None:
        for name, definition in (node.get("properties") or {}).items():
            where = f"{path}.{name}"
            if not isinstance(definition, dict):
                continue
            if not definition.get("description") and "$ref" not in definition:
                holes.append(where)
            walk(definition, where)

    walk(schema, schema.get("title", "root"))
    for name, definition in (schema.get("$defs") or {}).items():
        if isinstance(definition, dict):
            walk(definition, name)
    return sorted(holes)


def undocumented_models(schema: dict[str, Any]) -> list[str]:
    """Definitions with no description, i.e. models whose docstring never made it out."""
    missing = [
        name
        for name, definition in (schema.get("$defs") or {}).items()
        if isinstance(definition, dict) and not definition.get("description")
    ]
    if not schema.get("description"):
        missing.append(schema.get("title", "root"))
    return sorted(missing)


def write(directory: Path = DEFAULT_SCHEMA_DIR) -> list[Path]:
    """Write every exported schema as formatted JSON. Returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in EXPORTED.items():
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(json_schema(model), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
