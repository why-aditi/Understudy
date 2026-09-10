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
    from cua.discovery.runner import DiscoveryConfig, DiscoveryError, DiscoveryRunner, new_run_id
    from cua.discovery.tools import ClosedSchemaViolation
    from cua.evidence.logger import RunLogger
    from cua.llm.base import LLMClient
    from cua.llm.gemini import GeminiClient
    from cua.llm.groq import GroqClient
    from cua.llm.limiter import LLMError
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

    try:
        with RunLogger(run_id) as run_log, SessionRegistry(headless=headless) as sessions:
            # The CLI owns the registry, so the CLI opens the session. The run only attaches
            # to it, and never closes it: that inversion is the whole of C1.
            sessions.open(run_id)
            session = sessions.attach(run_id)
            session.lock.acquire("automation", by=run_id)

            runner = DiscoveryRunner(
                surface=WebSurface(session.page, session.lock),
                llm=llm,
                policy=engine,
                logger=run_log,
                config=config,
            )
            result = runner.run()
    except (DiscoveryError, ClosedSchemaViolation, LLMError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"{result.stop_reason} after {result.steps} steps -> {result.evidence_path}")
    if result.stop_reason != "goal_reached":
        raise typer.Exit(code=1)


@app.command()
def replay(
    capability: Annotated[str, typer.Option(help="Capability id to execute.")],
    params: Annotated[str, typer.Option(help="JSON object of capability parameters.")] = "{}",
    tenant: Annotated[str | None, typer.Option(help="Tenant overlay to apply.")] = None,
    offline: Annotated[bool, typer.Option(help="Replay against recorded fixtures.")] = False,
    attended: Annotated[
        bool, typer.Option(help="Permit replaying a draft capability, with a human watching.")
    ] = False,
    headless: Annotated[bool, typer.Option(help="Hide the browser.")] = False,
) -> None:
    """Execute a saved capability deterministically, with no model in the decision loop."""
    import json

    from cua.discovery.runner import new_run_id
    from cua.evidence.logger import RunLogger
    from cua.policy.engine import PolicyContext, PolicyEngine
    from cua.policy.rules import load_policy
    from cua.replay.engine import ReplayEngine, can_act_through, load_capability
    from cua.replay.resolver import Resolver
    from cua.session.registry import SessionRegistry
    from cua.surfaces.base import Action
    from cua.surfaces.web import WebSurface

    if offline:
        raise typer.BadParameter("--offline needs the recorded-fixture surface, which is not built")

    from cua.replay.engine import ReplayError, validate_params

    try:
        artifact = load_capability(capability)
        # Validated before a browser is launched: a misspelled parameter should cost
        # nothing, and the error the caller sees should be the one that matters.
        supplied = json.loads(params)
        validate_params(artifact.parameters, supplied)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: --params is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ReplayError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if tenant:
        from cua.replay.overlay import OverlayError, for_tenant

        try:
            resolved = for_tenant(artifact, tenant)
        except OverlayError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        artifact = resolved.capability
        if resolved.needs_review and not attended:
            # The engine would refuse this anyway - the overlay demoted it to draft - but it
            # would refuse with "state=draft", which does not tell the caller that an overlay
            # went stale. Said here, before a browser is launched, for the same reason bad
            # parameters are caught here: the message that matters should cost nothing.
            typer.echo(
                f"error: overlay needs review - {resolved.reason}. "
                "Re-verify it against this base version, or pass --attended.",
                err=True,
            )
            raise typer.Exit(code=2)
        if resolved.needs_review:
            typer.echo(f"warning: overlay needs review - {resolved.reason}", err=True)

    run_id = f"replay-{new_run_id()}"
    engine = PolicyEngine(load_policy())

    with RunLogger(run_id) as run_log, SessionRegistry(headless=headless) as sessions:
        sessions.open(run_id)
        session = sessions.attach(run_id)
        session.lock.acquire("automation", by=run_id)

        page = session.page
        surface = WebSurface(page, session.lock)
        # The entry point is an action like any other, so it goes through the chokepoint.
        entry = Action(kind="navigate", value=artifact.entry.url)
        verdict = engine.check(entry, PolicyContext(mode="replay", capability_id=artifact.id))
        if not verdict.allowed:
            raise typer.BadParameter(f"policy refused the entry point: {verdict.reason}")
        surface.act(entry)

        result = ReplayEngine(
            surface=surface,
            policy=engine,
            logger=run_log,
            resolver=Resolver(page=page, actionable=can_act_through),
        ).run(artifact, supplied, attended=attended)

    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    if result.status == "failure":
        raise typer.Exit(code=1)


@app.command()
def review(
    capability: Annotated[str, typer.Option(help="Capability id to review.")],
    repropose: Annotated[
        bool, typer.Option(help="Ask the model again instead of reusing cached proposals.")
    ] = False,
    by: Annotated[str, typer.Option(help="Who is reviewing. Recorded in provenance.")] = "",
    provider: Annotated[
        str, typer.Option(help="Provider for the proposal pass. Text-only, so groq by default.")
    ] = "groq",
) -> None:
    """Approve proposed outcomes and promote a capability from draft to approved.

    The proposals are a model's guesses and are advisory. Nothing reaches the capability
    without being accepted here.
    """
    import getpass
    from pathlib import Path

    from cua.recording.outcomes import (
        OutcomeProposal,
        apply_review,
        approve,
        describe_proposal,
        load_proposals,
        propose,
        save_proposals,
    )
    from cua.replay.engine import CAPABILITY_DIR, load_capability
    from cua.schema.models import Outcome

    artifact = load_capability(capability)
    directory = Path(CAPABILITY_DIR)
    reviewer = by or getpass.getuser()

    proposals = None if repropose else load_proposals(capability, directory)
    if proposals is None:
        from cua.llm.gemini import GeminiClient
        from cua.llm.groq import GroqClient

        llm = GroqClient() if provider == "groq" else GeminiClient()
        typer.echo(f"Asking {provider} what could go wrong in {capability}...")
        proposals = propose(artifact, llm)
        save_proposals(capability, proposals, directory)

    if not proposals:
        typer.echo("No proposals. Nothing to review.")
        raise typer.Exit(code=1)

    typer.echo(
        f"\n{len(proposals)} proposed outcome(s) for {capability}. These are guesses: the model "
        "has never seen this application fail.\n"
    )

    def decide(proposal: OutcomeProposal) -> Outcome | None:
        """Ask the reviewer. Accept as-is, reject, or edit the parts worth editing."""
        typer.echo(describe_proposal(proposal))
        choice = typer.prompt("  [a]ccept / [r]eject / [e]dit", default="r").strip().lower()[:1]
        if choice == "r":
            typer.echo("  rejected\n")
            return None

        outcome = proposal.outcome
        if choice == "e":
            outcome = outcome.model_copy(
                update={
                    "name": typer.prompt("  name", default=outcome.name),
                    "kind": typer.prompt("  kind", default=outcome.kind),
                    "message_template": typer.prompt("  message", default=outcome.message_template),
                }
            )
            key = next(iter(outcome.detect.params), None)
            if key is not None:
                params = dict(outcome.detect.params)
                params[key] = typer.prompt(f"  detect.{key}", default=str(params[key]))
                outcome = outcome.model_copy(
                    update={"detect": outcome.detect.model_copy(update={"params": params})}
                )
            # Re-validate: an edit can make a business outcome carry a recovery.
            outcome = Outcome.model_validate(outcome.model_dump(mode="json"))

        typer.echo(f"  accepted as {outcome.kind}/{outcome.name}\n")
        return outcome

    reviewed = apply_review(artifact, proposals, decide, approved_by=reviewer)
    kept = len(reviewed.outcomes) - len(artifact.outcomes)
    typer.echo(f"{kept} outcome(s) accepted, {len(proposals) - kept} rejected.")

    if typer.confirm("Promote this capability to approved?", default=False):
        reviewed = approve(reviewed, approved_by=reviewer)

    path = directory / f"{capability}.json"
    path.write_text(reviewed.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(
        f"saved {path} (state={reviewed.provenance.state}, "
        f"outcomes_reviewed={reviewed.provenance.outcomes_reviewed})"
    )


@app.command()
def stability(
    capability: Annotated[str, typer.Option(help="Capability id to measure.")],
    n: Annotated[int, typer.Option("-n", "--runs", help="Number of replay runs.")] = 10,
    params: Annotated[str, typer.Option(help="JSON object of capability parameters.")] = "{}",
    tenant: Annotated[str | None, typer.Option(help="Tenant overlay to apply.")] = None,
    headless: Annotated[bool, typer.Option(help="Hide the browser.")] = True,
    attended: Annotated[bool, typer.Option(help="Permit measuring a draft capability.")] = False,
    demote: Annotated[
        bool, typer.Option(help="Write the capability back to draft if drift crosses the bar.")
    ] = True,
) -> None:
    """Replay a capability N times and report pass rate and locator-strategy usage.

    Costs no model calls, which is the point: measuring determinism is only affordable
    because replay does not think.
    """
    import json

    from cua.discovery.runner import new_run_id
    from cua.evidence.logger import RunLogger
    from cua.policy.engine import PolicyContext, PolicyEngine
    from cua.policy.rules import load_policy
    from cua.replay import drift as drift_module
    from cua.replay.engine import (
        ReplayEngine,
        ReplayError,
        can_act_through,
        load_capability,
        save_capability,
        validate_params,
    )
    from cua.replay.resolver import Resolver
    from cua.replay.stability import measure, render, write_report
    from cua.schema.models import ReplayResult
    from cua.session.registry import SessionRegistry
    from cua.surfaces.base import Action
    from cua.surfaces.web import WebSurface

    try:
        artifact = load_capability(capability)
        supplied = json.loads(params)
        validate_params(artifact.parameters, supplied)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: --params is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ReplayError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if tenant:
        from cua.replay.overlay import OverlayError, for_tenant

        try:
            resolved = for_tenant(artifact, tenant)
        except OverlayError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        artifact = resolved.capability
        if resolved.needs_review:
            typer.echo(f"warning: overlay needs review - {resolved.reason}", err=True)

    engine = PolicyEngine(load_policy())
    session_id = f"stability-{new_run_id()}"

    with SessionRegistry(headless=headless) as sessions:
        sessions.open(session_id)
        session = sessions.attach(session_id)
        session.lock.acquire("automation", by=session_id)
        surface = WebSurface(session.page, session.lock)

        def replay_once(attempt: int) -> ReplayResult:
            """One run, from the entry point, into its own evidence directory."""
            with RunLogger(f"{session_id}-run{attempt + 1:02d}") as run_log:
                entry = Action(kind="navigate", value=artifact.entry.url)
                verdict = engine.check(
                    entry, PolicyContext(mode="replay", capability_id=artifact.id)
                )
                if not verdict.allowed:
                    raise typer.BadParameter(f"policy refused the entry point: {verdict.reason}")
                surface.act(entry)
                return ReplayEngine(
                    surface=surface,
                    policy=engine,
                    logger=run_log,
                    resolver=Resolver(page=session.page, actionable=can_act_through),
                ).run(artifact, supplied, attended=attended)

        typer.echo(f"replaying {capability} {n} times...")
        report = measure(artifact, replay_once, runs=n)

    path = write_report(report)
    typer.echo("")
    typer.echo(render(report))

    verdict = drift_module.assess(artifact, report)
    typer.echo("")
    typer.echo(drift_module.render(verdict))
    verdict_path = path.with_name(f"drift-x{report.runs}.json")
    verdict_path.write_text(verdict.model_dump_json(indent=2), encoding="utf-8")

    if verdict.demote and demote:
        if tenant:
            # A resolved overlay is not an artifact on disk, so there is nothing to write
            # back. Demoting the base because a tenant's overlay has rotted would blame
            # the wrong thing - the overlay is what needs review.
            typer.echo("  (tenant run: the overlay needs review, the base is untouched)")
        else:
            save_capability(drift_module.demote(artifact, verdict, report))

    typer.echo("")
    typer.echo(f"written to {path} and {verdict_path}")
    if report.passes < report.runs:
        raise typer.Exit(code=1)


@app.command()
def operator(
    host: Annotated[str, typer.Option(help="Bind address for the operator console.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for the operator console.")] = 8765,
) -> None:
    """Serve the mocked operator console that displays pending intervention requests.

    The UI is the mock. The lock, the control transfer and the capture are real.
    """
    from pathlib import Path

    from cua.escalation.operator_app import serve
    from cua.evidence.logger import EVIDENCE_ROOT

    typer.echo(f"operator console on http://{host}:{port}  (evidence: {Path(EVIDENCE_ROOT)})")
    typer.echo("No authentication: bind to localhost only.")
    serve(host=host, port=port, evidence_root=Path(EVIDENCE_ROOT))
