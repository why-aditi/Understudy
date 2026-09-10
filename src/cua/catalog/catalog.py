"""Capability catalog: saved artifacts, exposed to an agent as callable typed tools.

This is where the project meets a real agent. Everything else produces an artifact; this
turns a directory of artifacts into a tool list a model can be handed, and turns the model's
tool call back into a deterministic replay.

The division of labour is the entire thesis, so it is worth stating exactly:

  the model chooses    which capability to call, and what arguments to pass
  the model does not   decide a single action inside that capability

A tool call crosses from the first into the second and never comes back. `call()` validates
the arguments, hands them to replay, and returns a `ReplayResult`. Nothing between those two
points consults a model - and this module imports no provider, so an agent integration is
something a caller assembles rather than something the catalog assumes.

Only capabilities a human has approved are listed. `Provenance.replayable_unattended` is the
same gate that governs unattended replay and tenant overlay staleness: an agent calling a
tool unsupervised *is* the unattended case, so it is the same question with the same answer.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from cua.replay.engine import CAPABILITY_DIR, ParameterError, validate_params
from cua.schema.models import Capability, ParameterType, ReplayResult

_log = logging.getLogger(__name__)

# The artifact's declared types, in the vocabulary JSON Schema uses. `date` is the one that
# does not map one-to-one: JSON Schema has no date type, only a string with a format.
JSON_TYPE: dict[ParameterType, dict[str, str]] = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
}

# Where the full envelope is defined, for an integrator who wants more than the summary the
# output schema carries inline.
RESULT_SCHEMA = "capabilities/schema/replay-result.schema.json"


class CatalogError(Exception):
    """A tool could not be called. Never a silent no-op."""


class UnknownTool(CatalogError):
    """No capability by that name. Distinct from one that exists but is not callable."""


class NotApproved(CatalogError):
    """The capability exists but no human has signed it off, so an agent may not call it."""


class CapabilityTool(BaseModel):
    """One capability, described the way a tool-calling model expects to be told about it."""

    name: str = Field(description="The capability id, which is also the tool name.")
    description: str = Field(description="What it does, written for a model choosing a tool.")
    capability_version: str = Field(description="Version of the artifact behind this tool.")
    tenant_id: str | None = Field(default=None, description="Tenant the artifact was recorded on.")
    input_schema: dict[str, Any] = Field(
        description="JSON Schema for the arguments, derived from the declared parameters."
    )
    output_schema: dict[str, Any] = Field(
        description="JSON Schema for the result, derived from the declared outputs."
    )


# ---- schema generation ------------------------------------------------------------------------


def input_schema(capability: Capability) -> dict[str, Any]:
    """The argument schema, straight off the capability's declared parameters."""
    properties: dict[str, Any] = {}
    for parameter in capability.parameters:
        prop: dict[str, Any] = {
            **JSON_TYPE[parameter.type],
            "description": parameter.description,
        }
        if parameter.example is not None:
            prop["examples"] = [parameter.example]
        if parameter.sensitive:
            # Said in the contract because the caller is the only one who ever holds this:
            # it is supplied per invocation and never written down anywhere.
            prop["description"] += " Sensitive: supplied per call, never persisted."
            prop["writeOnly"] = True
        properties[parameter.name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": [p.name for p in capability.parameters if p.required],
        # Matches what validation actually does. A caller who misspells a parameter should
        # be told, not watched while the capability runs happily on a default.
        "additionalProperties": False,
    }


def output_schema(capability: Capability) -> dict[str, Any]:
    """The result schema: the replay envelope, with `outputs` typed per declared output.

    An agent needs the envelope, not just the payload. "No such member" arrives as
    `status=business_outcome` with an `outcome`, and telling that apart from a failure
    without parsing a message is the thing the artifact exists to make possible.
    """
    outputs: dict[str, Any] = {
        "type": "object",
        "description": "The declared outputs, present when status is success.",
        "properties": {
            output.name: {**JSON_TYPE[output.type], "description": output.description}
            for output in capability.outputs
        },
        "required": [output.name for output in capability.outputs],
    }
    return {
        "type": "object",
        "description": (
            "A ReplayResult. `success` carries outputs; `business_outcome` is a legitimate "
            "answer the capability recognised, not an error; `failure` means the automation "
            "broke and carries debuggable detail."
        ),
        "properties": {
            "status": {
                "enum": ["success", "business_outcome", "failure"],
                "description": "Which of the three kinds of ending this run had.",
            },
            "outputs": {
                "anyOf": [outputs, {"type": "null"}],
                "description": "Declared outputs on success, null otherwise.",
            },
            "outcome": {
                "type": ["object", "null"],
                "description": "The recognised outcome that fired, when one did.",
                "properties": {
                    "name": {"type": "string", "description": "Outcome identifier."},
                    "kind": {
                        "enum": ["business", "recoverable", "hard_failure"],
                        "description": "Which class of outcome it is.",
                    },
                    "message": {"type": "string", "description": "Explanation for the caller."},
                },
            },
            "failure": {
                "type": ["object", "null"],
                "description": "Debuggable detail, present only when status is failure.",
            },
        },
        "required": ["status"],
        "$comment": f"Full envelope: {RESULT_SCHEMA}",
    }


def to_tool(capability: Capability) -> CapabilityTool:
    """One approved capability as a callable tool declaration."""
    return CapabilityTool(
        name=capability.id,
        description=capability.description,
        capability_version=capability.version,
        tenant_id=capability.app.tenant_id,
        input_schema=input_schema(capability),
        output_schema=output_schema(capability),
    )


# ---- discovery --------------------------------------------------------------------------------


def discover(directory: Path = CAPABILITY_DIR) -> list[Capability]:
    """Every saved capability in `directory`, newest-first by id for a stable listing.

    Files that are not capabilities are skipped with a log line rather than an exception:
    the directory also holds outcome proposals and the exported schemas, and a catalog that
    refused to load because of a neighbouring file would be useless. Skipping *quietly*
    would be the bug, so each skip says which file and why.
    """
    found: list[Capability] = []
    for path in sorted(directory.glob("*.json")):
        try:
            found.append(Capability.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, ValidationError) as exc:
            _log.info(
                "catalog_skipped_file",
                extra={"path": str(path), "why": type(exc).__name__},
            )
    return found


# ---- the catalog ------------------------------------------------------------------------------

Invoke = Callable[[Capability, dict[str, Any]], ReplayResult]


class Catalog:
    """Approved capabilities, callable by name with typed arguments.

    `invoke` performs the replay. Injecting it keeps the catalog free of browsers, sessions
    and policy - the same inversion C1 asks of every other caller - and makes the whole
    dispatch path testable without launching anything.
    """

    def __init__(self, capabilities: list[Capability], invoke: Invoke) -> None:
        self._capabilities = {capability.id: capability for capability in capabilities}
        self._invoke = invoke

    @classmethod
    def from_directory(cls, invoke: Invoke, directory: Path = CAPABILITY_DIR) -> "Catalog":
        return cls(discover(directory), invoke)

    def tools(self) -> list[CapabilityTool]:
        """The tool list to hand a model. Approved capabilities only."""
        return [
            to_tool(capability)
            for capability in sorted(self._capabilities.values(), key=lambda c: c.id)
            if capability.provenance.replayable_unattended
        ]

    def describe(self, name: str) -> CapabilityTool:
        """One tool by name, refusing for a reason rather than returning nothing."""
        return to_tool(self._approved(name))

    def call(
        self, name: str, arguments: dict[str, Any], *, tenant: str | None = None
    ) -> ReplayResult:
        """Invoke a capability by name. The crossing from model-chosen to model-free.

        Arguments are validated and coerced to their declared types before anything runs,
        so a model that emits "12345" for an integer parameter is corrected here rather
        than typed verbatim into a form.
        """
        capability = self._approved(name)
        if tenant is not None:
            from cua.replay.overlay import for_tenant

            resolved = for_tenant(capability, tenant)
            if resolved.needs_review:
                raise NotApproved(
                    f"the {tenant!r} overlay for {name!r} needs review: {resolved.reason}"
                )
            capability = resolved.capability

        try:
            validated = validate_params(capability.parameters, arguments)
        except ParameterError as exc:
            raise CatalogError(f"{name}: {exc}") from exc

        # Names only. A sensitive argument must not reach a log even here, and the redaction
        # filter is the backstop rather than the plan.
        _log.info(
            "catalog_call",
            extra={
                "capability_id": capability.id,
                "capability_version": capability.version,
                "tenant": capability.app.tenant_id,
                "argument_names": sorted(validated),
            },
        )
        return self._invoke(capability, validated)

    def _approved(self, name: str) -> Capability:
        capability = self._capabilities.get(name)
        if capability is None:
            raise UnknownTool(f"no capability named {name!r}; have {sorted(self._capabilities)}")
        if not capability.provenance.replayable_unattended:
            raise NotApproved(
                f"{name!r} is not callable: state={capability.provenance.state}, "
                f"outcomes_reviewed={capability.provenance.outcomes_reviewed}. "
                "A human has to approve it and review its outcomes first."
            )
        return capability
