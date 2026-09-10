"""Smoke tests: the CLI surface exists and every subcommand is wired but unimplemented."""

import pytest
from typer.testing import CliRunner

from cua.cli import app

COMMANDS = ["discover", "replay", "review", "stability", "operator"]

runner = CliRunner()


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in COMMANDS:
        assert name in result.output


@pytest.mark.parametrize(
    ("argv"),
    [
        ["replay", "--capability", "member.balance.lookup"],
        ["review", "--capability", "member.balance.lookup"],
        ["stability", "--capability", "member.balance.lookup"],
        ["operator"],
    ],
)
def test_command_raises_not_implemented(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert isinstance(result.exception, NotImplementedError)


def test_discover_is_wired_to_the_runner() -> None:
    """discover is implemented, so it fails on a real precondition, not NotImplementedError."""
    result = runner.invoke(app, ["discover", "--goal", "g", "--target", "http://localhost:8080"])
    assert not isinstance(result.exception, NotImplementedError)
