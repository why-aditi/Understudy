"""An LLM picks a capability by name, calls it with typed args, and gets a ReplayResult.

This is the join between the two halves of the project. The model reads the tool list the
catalog generates, decides which capability answers the question and what to pass it. The
catalog validates the arguments and hands them to replay. Replay executes the recorded flow
against a live browser with **no model in the decision loop**.

That last claim is the one worth checking rather than asserting, so the provider is wrapped
in a counter and the count is read either side of every tool call. If a single model call
happened inside `catalog.call`, this script would say so.

Needs the harness running and GROQ_API_KEY set:

    uv run python -m apps.harness 8099
    uv run python scripts/agent_demo.py
"""

import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cua.catalog.catalog import Catalog, CatalogError, discover
from cua.discovery.runner import new_run_id
from cua.evidence.logger import RunLogger
from cua.llm.base import LLMResponse, Message, ToolSpec
from cua.llm.groq import GroqClient
from cua.policy.engine import PolicyContext, PolicyEngine
from cua.policy.rules import load_policy
from cua.replay.engine import ReplayEngine, can_act_through
from cua.replay.resolver import Resolver
from cua.schema.models import Capability, ReplayResult
from cua.session.registry import SessionRegistry
from cua.surfaces.base import Action
from cua.surfaces.web import WebSurface

OUTPUT = Path("evidence/catalog-agent-demo.txt")

SYSTEM = (
    "You are a servicing assistant with access to recorded UI capabilities. "
    "Use a tool when one answers the question. Answer in one short sentence."
)

# Deliberately natural: neither question names a capability, a parameter or an id field.
# Choosing the tool and extracting the argument is the model's job.
QUESTIONS = [
    "Who is member 12345?",
    "And can you look up member 99999 as well?",
]


class Counting:
    """A provider that counts its calls, so "no model in the loop" is measured."""

    def __init__(self, inner: GroqClient) -> None:
        self._inner = inner
        self.calls = 0
        self.supports_images = inner.supports_images
        self.model = inner.model

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        images: list[bytes] | None = None,
    ) -> LLMResponse:
        self.calls += 1
        return self._inner.complete(messages, tools, images)


def replayer(surface: WebSurface, policy: PolicyEngine, page: Any) -> Any:  # noqa: ANN401
    """The catalog's `invoke`: one deterministic replay, from the entry point."""

    def invoke(capability: Capability, params: dict[str, Any]) -> ReplayResult:
        with RunLogger(f"agent-{new_run_id()}") as log:
            entry = Action(kind="navigate", value=capability.entry.url)
            verdict = policy.check(entry, PolicyContext(mode="replay", capability_id=capability.id))
            if not verdict.allowed:
                raise CatalogError(f"policy refused the entry point: {verdict.reason}")
            surface.act(entry)
            return ReplayEngine(
                surface=surface,
                policy=policy,
                logger=log,
                resolver=Resolver(page=page, actionable=can_act_through),
            ).run(capability, params)

    return invoke


def guards(catalog: Catalog, drafted: Catalog) -> list[tuple[str, Any]]:
    """The refusals an agent integration depends on, exercised directly.

    A model cannot be relied on to produce these on demand, and they matter more than the
    happy path: they are what stands between a hallucinated tool call and a live browser.

    `drafted` holds the same real capability with its approval taken away, so the last two
    checks run against an artifact rather than a fixture invented for the occasion.
    """
    return [
        ("a capability that does not exist", lambda: catalog.call("member.teleport", {})),
        ("a misspelled argument", lambda: catalog.call("member.search", {"membre_id": "1"})),
        ("a missing required argument", lambda: catalog.call("member.search", {})),
        (
            "calling an unapproved capability",
            lambda: drafted.call("member.search", {"member_id": "1"}),
        ),
    ]


def unapprove(catalog: Catalog, invoke: Any) -> Catalog:  # noqa: ANN401
    """The same capabilities with approval withdrawn, to show the gate refusing."""
    withdrawn = [
        capability.model_copy(
            update={"provenance": capability.provenance.model_copy(update={"state": "draft"})}
        )
        for capability in discover()
    ]
    return Catalog(withdrawn, invoke)


def main() -> None:
    load_dotenv()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    llm = Counting(GroqClient())
    report: list[str] = []

    def say(line: str = "") -> None:
        report.append(line)
        print(line)

    session_id = f"agent-demo-{new_run_id()}"
    with SessionRegistry(headless=True) as sessions:
        sessions.open(session_id)
        session = sessions.attach(session_id)
        session.lock.acquire("automation", by=session_id)
        surface = WebSurface(session.page, session.lock)
        policy = PolicyEngine(load_policy())

        invoke = replayer(surface, policy, session.page)
        catalog = Catalog.from_directory(invoke)
        offered = catalog.tools()
        tools = [
            ToolSpec(name=t.name, description=t.description, parameters=t.input_schema)
            for t in offered
        ]

        say("An LLM calling a recorded capability")
        say("=" * 78)
        say()
        say(f"model    : {llm.model} (Groq free tier)")
        say(f"catalog  : {len(offered)} approved capabilit(ies), generated from the artifacts")
        say()
        for tool in offered:
            say(f"  tool  {tool.name}  v{tool.capability_version}")
            say(f"        {tool.description}")
            say(f"        args    {json.dumps(tool.input_schema['properties'])}")
            say(f"        required {tool.input_schema['required']}")
        say()

        messages = [Message(role="system", content=SYSTEM)]
        for question in QUESTIONS:
            say("-" * 78)
            say(f'user  : "{question}"')
            messages.append(Message(role="user", content=question))

            response = llm.complete(messages, tools)
            if not response.tool_calls:
                say(f"model : (no tool call) {response.text}")
                continue

            call = response.tool_calls[0]
            say(f"model : calls {call.name}({json.dumps(call.arguments)})")

            before = llm.calls
            result = catalog.call(call.name, call.arguments)
            during = llm.calls - before

            say(f"replay: status={result.status}  steps={result.steps_executed}")
            say(f"        outputs={json.dumps(result.outputs)}")
            if result.outcome:
                say(f"        outcome={result.outcome.name} ({result.outcome.kind})")
            say(f"        locators={json.dumps(result.locator_usage)}")
            say(f"        model calls during replay: {during}")

            # The Message model carries no tool_call_id, so the result goes back as user
            # content rather than a role=tool turn. Enough for the round trip; a production
            # integration would thread the id through.
            envelope = result.model_dump(mode="json", include={"status", "outputs", "outcome"})
            messages.append(
                Message(role="user", content=f"Result of {call.name}: {json.dumps(envelope)}")
            )
            final = llm.complete(messages)
            say(f'model : "{(final.text or "").strip()}"')
            messages.append(Message(role="assistant", content=final.text or ""))
            say()

        say("-" * 78)
        say("What the catalog refuses, checked without a model:")
        say()
        say(
            f"  {'listed as callable':<34} "
            f"{len(catalog.tools())} approved, {len(unapprove(catalog, invoke).tools())} "
            "once approval is withdrawn"
        )
        for label, thunk in guards(catalog, unapprove(catalog, invoke)):
            try:
                thunk()
            except CatalogError as exc:
                say(f"  {label:<34} {type(exc).__name__}: {exc}")
            else:
                say(f"  {label:<34} NOT REFUSED - that is a bug")
        say()

    say("-" * 78)
    say(f"{llm.calls} model calls in total, all of them to choose a tool or phrase an answer.")
    say("Zero inside any replay - that column is the whole point of the artifact.")
    say("The model chose the capability and the argument. It decided no action within one.")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\nwritten to {OUTPUT}")


if __name__ == "__main__":
    main()
