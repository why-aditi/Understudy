"""Smoke tests: the CLI surface exists and every subcommand is wired."""

from pathlib import Path

from typer.testing import CliRunner

from cua.cli import app

COMMANDS = ["discover", "replay", "review", "stability", "operator"]

runner = CliRunner()


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in COMMANDS:
        assert name in result.output


def test_no_subcommand_is_still_a_stub() -> None:
    """Every command in the CLI surface is implemented; none raises NotImplementedError.

    `operator` is excluded from invocation on purpose: serve() blocks forever by design.
    """
    for name in COMMANDS:
        if name == "operator":
            continue
        result = runner.invoke(app, [name, "--capability", "nope", "--goal", "g", "--target", "t"])
        assert not isinstance(result.exception, NotImplementedError), name


def test_stability_is_wired_to_the_measurement() -> None:
    result = runner.invoke(app, ["stability", "--capability", "nope.does.not.exist"])
    assert result.exit_code == 2
    assert "cannot read capability" in result.output


def test_operator_is_wired_to_the_console() -> None:
    """operator is implemented. It is not invoked here: serve() blocks forever by design."""
    from cua.escalation.operator_app import create_app, serve

    assert callable(serve)
    assert create_app(Path("evidence")) is not None


def test_review_is_wired_to_the_proposal_pass() -> None:
    """review is implemented, so it fails on a real precondition, not NotImplementedError."""
    result = runner.invoke(app, ["review", "--capability", "nope.does.not.exist"])
    assert not isinstance(result.exception, NotImplementedError)


def test_replay_is_wired_to_the_engine() -> None:
    """replay is implemented, so it fails on a real precondition, not NotImplementedError."""
    result = runner.invoke(app, ["replay", "--capability", "nope.does.not.exist"])
    assert not isinstance(result.exception, NotImplementedError)


def test_discover_is_wired_to_the_runner() -> None:
    """discover is implemented, so it fails on a real precondition, not NotImplementedError."""
    result = runner.invoke(app, ["discover", "--goal", "g", "--target", "http://localhost:8080"])
    assert not isinstance(result.exception, NotImplementedError)


# ---- caller mistakes are messages, not stack traces ------------------------------------


def test_a_missing_capability_is_a_clean_error() -> None:
    result = runner.invoke(app, ["replay", "--capability", "nope.does.not.exist"])
    assert result.exit_code == 2
    assert "cannot read capability" in result.output
    assert "Traceback" not in result.output


def test_malformed_params_are_a_clean_error() -> None:
    result = runner.invoke(app, ["replay", "--capability", "member.search", "--params", "not json"])
    assert result.exit_code == 2
    assert "not valid JSON" in result.output


def test_an_unknown_parameter_is_refused_before_a_browser_is_launched() -> None:
    """A caller's typo should cost nothing: no session, no browser, no evidence directory."""
    result = runner.invoke(
        app,
        ["replay", "--capability", "member.search", "--params", '{"wrong": "x"}'],
    )
    assert result.exit_code == 2
    assert "unknown parameter(s) ['wrong']" in result.output


def test_an_unknown_tenant_overlay_is_a_clean_error() -> None:
    result = runner.invoke(
        app,
        [
            "replay",
            "--capability",
            "member.search",
            "--params",
            '{"member_id": "12345"}',
            "--tenant",
            "tenant-nowhere",
        ],
    )
    assert result.exit_code == 2
    assert "no overlay for" in result.output
    assert "Traceback" not in result.output


def test_a_stale_overlay_is_refused_before_a_browser_is_launched() -> None:
    """The committed stale fixture: the refusal names the overlay, not just 'state=draft'.

    Letting the engine's precondition catch this would report `state=draft` after paying
    for a browser, which tells the caller nothing about *why* it was demoted.
    """
    result = runner.invoke(
        app,
        [
            "replay",
            "--capability",
            "member.search",
            "--params",
            '{"member_id": "12345"}',
            "--tenant",
            "tenant-b-stale",
        ],
    )
    assert result.exit_code == 2
    assert "overlay needs review" in result.output
    assert "verified against 0.9.0" in result.output
    assert "--attended" in result.output, "the way forward is named, not just the refusal"
