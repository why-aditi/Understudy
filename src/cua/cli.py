"""Typer entry point exposing the discover, replay, review, stability, and operator commands."""

from typing import Annotated

import typer
from dotenv import load_dotenv

app = typer.Typer(
    name="cua",
    help="Discover a UI capability with an LLM, record it, replay it deterministically.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Load .env before any subcommand runs, so API keys never live in the shell."""
    load_dotenv()


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal to accomplish.")],
    target: Annotated[str, typer.Option(help="Entry-point URL of the target surface.")],
    tenant: Annotated[
        str, typer.Option(help="Tenant id the run is recorded against.")
    ] = "tenant-a",
    max_steps: Annotated[int, typer.Option(help="Hard ceiling on loop iterations.")] = 25,
    timeout: Annotated[float, typer.Option(help="Wall-clock budget in seconds.")] = 300.0,
    allow_screenshots: Annotated[
        bool,
        typer.Option(
            help="Capture a screenshot per step into the evidence directory. "
            "Synthetic targets only. A vision-capable provider also sees them."
        ),
    ] = False,
    headless: Annotated[
        bool, typer.Option(help="Hide the browser. The escalation demo needs it visible.")
    ] = False,
    provider: Annotated[
        str, typer.Option(help="LLM provider: gemini (vision) or groq (text-only).")
    ] = "gemini",
) -> None:
    """Run the LLM observe/decide/act loop and emit a draft capability artifact."""
    from cua.discovery.runner import DiscoveryConfig, DiscoveryRunner, new_run_id
    from cua.evidence.logger import RunLogger
    from cua.llm.base import LLMClient
    from cua.llm.gemini import GeminiClient
    from cua.llm.groq import GroqClient
    from cua.policy.engine import PolicyEngine
    from cua.policy.rules import load_policy
    from cua.session.registry import SessionRegistry
    from cua.surfaces.web import WebSurface

    run_id = f"discovery-{new_run_id()}"
    config = DiscoveryConfig(
        goal=goal,
        target=target,
        tenant=tenant,
        max_steps=max_steps,
        wall_clock_seconds=timeout,
        screenshots=allow_screenshots,
    )
    engine = PolicyEngine(load_policy())

    if provider == "gemini":
        llm: LLMClient = GeminiClient()
    elif provider == "groq":
        llm = GroqClient()
    else:
        raise typer.BadParameter(f"unknown provider {provider!r}; use gemini or groq")

    with RunLogger(run_id) as run_log, SessionRegistry(headless=headless) as sessions:
        runner = DiscoveryRunner(
            surface=WebSurface(sessions.attach(run_id)),
            llm=llm,
            policy=engine,
            logger=run_log,
            config=config,
        )
        result = runner.run()

    typer.echo(f"{result.stop_reason} after {result.steps} steps -> {result.evidence_path}")
    if result.stop_reason != "goal_reached":
        raise typer.Exit(code=1)


@app.command()
def replay(
    capability: Annotated[str, typer.Option(help="Capability id to execute.")],
    params: Annotated[str, typer.Option(help="JSON object of capability parameters.")] = "{}",
    tenant: Annotated[str | None, typer.Option(help="Tenant overlay to apply.")] = None,
    offline: Annotated[bool, typer.Option(help="Replay against recorded fixtures.")] = False,
) -> None:
    """Execute a saved capability deterministically, with no model in the decision loop."""
    raise NotImplementedError


@app.command()
def review(
    capability: Annotated[str, typer.Option(help="Capability id to review.")],
) -> None:
    """Approve proposed outcomes and promote a capability from draft to approved."""
    raise NotImplementedError


@app.command()
def stability(
    capability: Annotated[str, typer.Option(help="Capability id to measure.")],
    n: Annotated[int, typer.Option("-n", "--runs", help="Number of replay runs.")] = 10,
) -> None:
    """Replay a capability N times and report pass rate and locator-strategy usage."""
    raise NotImplementedError


@app.command()
def operator(
    host: Annotated[str, typer.Option(help="Bind address for the operator console.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for the operator console.")] = 8765,
) -> None:
    """Serve the mocked operator console that displays pending intervention requests."""
    raise NotImplementedError
