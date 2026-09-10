"""Offline replay: the deterministic half, exercised with no browser and no network.

The point of this path is that a reviewer with no API keys and no Chromium can still check
the central claim. So the tests that matter are the ones that would catch it quietly cheating:
that no socket is opened, and that a tape refuses to play a run that diverges from the one it
recorded. A fixture surface that accepted whatever it was handed would pass every time and
prove nothing.
"""

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cua.cli import app
from cua.surfaces.base import Action, ActionResult, ActionTarget, AXNode, Observation, PruningStats
from cua.surfaces.fixture import (
    Fixture,
    FixtureError,
    FixtureSurface,
    Frame,
    RecordingSurface,
    fixture_path,
    load_fixture,
    save_fixture,
)

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

PARAMS = '{"member_id": "12345"}'


def observation(name: str = "Member search") -> Observation:
    return Observation(
        url="http://127.0.0.1:8099/tenant-a/",
        title=name,
        tree=AXNode(role="RootWebArea", name=name, children=[AXNode(role="link", name="Search")]),
        observation_hash="h",
        pruning=PruningStats(nodes_before=9, nodes_after=2),
    )


def click(name: str = "Search") -> Action:
    return Action(kind="click", target=ActionTarget(role="link", name=name))


def result_of(action: Action, extracted: str | None = None) -> ActionResult:
    return ActionResult(
        action=action,
        ok=True,
        url_after="http://127.0.0.1:8099/tenant-a/members",
        duration_ms=5,
        extracted=extracted,
    )


def tape(*frames: Frame, params: dict[str, Any] | None = None) -> Fixture:
    return Fixture(
        capability_id="member.search",
        capability_version="1.0.0",
        params=params if params is not None else {"member_id": "12345"},
        entry_url="http://127.0.0.1:8099/tenant-a/",
        frames=list(frames),
    )


# ---- playback ---------------------------------------------------------------------------------


def test_observe_returns_the_recorded_tree() -> None:
    surface = FixtureSurface(tape(Frame(kind="observe", observation=observation())))

    assert surface.observe().tree is not None
    assert surface.exhausted


def test_act_returns_the_recorded_result_for_the_recorded_action() -> None:
    action = click()
    surface = FixtureSurface(tape(Frame(kind="act", action=action, result=result_of(action, "x"))))

    assert surface.act(click()).extracted == "x"


def test_a_different_action_at_the_same_position_fails_loudly() -> None:
    """The whole value of the tape. A lenient surface would pass and prove nothing."""
    recorded = click("Search")
    surface = FixtureSurface(tape(Frame(kind="act", action=recorded, result=result_of(recorded))))

    with pytest.raises(FixtureError, match="resolved a different control"):
        surface.act(click("Open"))


def test_the_mismatch_message_names_both_actions() -> None:
    recorded = Action(kind="type", target=ActionTarget(role="textbox", name="Member ID"), value="1")
    surface = FixtureSurface(tape(Frame(kind="act", action=recorded, result=result_of(recorded))))

    with pytest.raises(FixtureError) as caught:
        surface.act(
            Action(kind="type", target=ActionTarget(role="textbox", name="Member ID"), value="2")
        )

    assert "'1'" in str(caught.value)
    assert "'2'" in str(caught.value)


def test_a_differing_timeout_is_still_the_same_action() -> None:
    """Timing is not identity: a slower run replays the same tape."""
    recorded = click()
    surface = FixtureSurface(tape(Frame(kind="act", action=recorded, result=result_of(recorded))))

    assert surface.act(click().model_copy(update={"timeout_ms": 30_000})).ok


def test_asking_for_an_act_where_an_observe_was_recorded_fails() -> None:
    surface = FixtureSurface(tape(Frame(kind="observe", observation=observation())))

    with pytest.raises(FixtureError, match="different path"):
        surface.act(click())


def test_running_off_the_end_of_the_tape_says_so() -> None:
    surface = FixtureSurface(tape())

    with pytest.raises(FixtureError, match="tape ran out"):
        surface.observe()


def test_a_screenshot_request_is_accepted_and_ignored() -> None:
    """A tape carries a tree, never pixels. Offline evidence is the tree."""
    surface = FixtureSurface(tape(Frame(kind="observe", observation=observation())))

    assert surface.observe(screenshot=True).screenshot is None


# ---- recording --------------------------------------------------------------------------------


class Fake:
    """A minimal live surface to record from."""

    def observe(self, *, screenshot: bool = False) -> Observation:
        return observation()

    def act(self, action: Action) -> ActionResult:
        return result_of(action, "4,182.55")


def test_recording_captures_observes_and_acts_in_order() -> None:
    recorder = RecordingSurface(Fake())

    recorder.observe()
    recorder.act(click())
    recorder.observe()

    assert [frame.kind for frame in recorder.frames] == ["observe", "act", "observe"]


def test_a_recording_replays_back_through_the_fixture_surface() -> None:
    recorder = RecordingSurface(Fake())
    recorder.observe()
    recorder.act(click())

    played = FixtureSurface(tape(*recorder.frames))

    assert played.observe().title == "Member search"
    assert played.act(click()).extracted == "4,182.55"


def test_a_fixture_round_trips_through_its_file(tmp_path: Path) -> None:
    written = tape(Frame(kind="observe", observation=observation()))

    save_fixture(written, tmp_path)

    assert load_fixture("member.search", tmp_path) == written


def test_a_saved_fixture_carries_no_screenshot_bytes(tmp_path: Path) -> None:
    """Screenshots are excluded from the dump, so a tape cannot leak pixels to disk."""
    shot = observation().model_copy(update={"screenshot": b"\x89PNG not really"})
    save_fixture(tape(Frame(kind="observe", observation=shot)), tmp_path)

    raw = fixture_path("member.search", tmp_path).read_text(encoding="utf-8")

    assert "screenshot" not in raw
    assert "PNG" not in raw


def test_a_missing_fixture_says_how_to_record_one(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="--record-fixture"):
        load_fixture("member.nope", tmp_path)


def test_a_malformed_fixture_is_not_silently_skipped(tmp_path: Path) -> None:
    fixture_path("member.search", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    fixture_path("member.search", tmp_path).write_text('{"frames": []}', encoding="utf-8")

    with pytest.raises(FixtureError, match="not a valid recording"):
        load_fixture("member.search", tmp_path)


# ---- the committed fixture, end to end through the CLI -------------------------------------


def test_the_committed_fixture_replays_offline() -> None:
    """What a reviewer with no keys and no Chromium actually runs."""
    result = runner.invoke(
        app, ["replay", "--capability", "member.search", "--params", PARAMS, "--offline"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "success"
    assert payload["outputs"] == {"member_name": "Wilhelmina Okonkwo-Bright"}
    assert payload["locator_usage"] == {
        "enter-id": "role_name",
        "submit-search": "role_name",
        # The name cell resolves through an anchor bound to member_id, not through a
        # role_name on the member's own name - which only ever worked for one member.
        "read-name": "anchor_relative",
    }


def test_offline_replay_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim, enforced. Reading the tape is the only I/O this path performs."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline replay attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    result = runner.invoke(
        app, ["replay", "--capability", "member.search", "--params", PARAMS, "--offline"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "success"


def test_offline_matches_the_live_run_it_was_recorded_from() -> None:
    """The recorded result is in the tape's own last frames; the replay must agree with it."""
    fixture = load_fixture("member.search", REPO / "fixtures")
    extracted = [f.result.extracted for f in fixture.frames if f.result and f.result.extracted]

    result = runner.invoke(
        app, ["replay", "--capability", "member.search", "--params", PARAMS, "--offline"]
    )

    assert json.loads(result.output)["outputs"]["member_name"] in extracted


def test_offline_with_different_params_is_refused_rather_than_faked() -> None:
    """A tape bakes in the values typed into the surface. It cannot answer another question."""
    result = runner.invoke(
        app,
        [
            "replay",
            "--capability",
            "member.search",
            "--params",
            '{"member_id": "67890"}',
            "--offline",
        ],
    )

    assert result.exit_code == 2
    assert "recorded with params" in result.output


def test_offline_for_a_capability_with_no_fixture_is_a_clean_error() -> None:
    result = runner.invoke(
        app, ["replay", "--capability", "member.savings_balance.read", "--offline"]
    )

    assert result.exit_code == 2
    assert "no offline fixture" in result.output
    assert "Traceback" not in result.output


def test_recording_and_replaying_at_once_is_refused() -> None:
    result = runner.invoke(
        app,
        [
            "replay",
            "--capability",
            "member.search",
            "--params",
            PARAMS,
            "--offline",
            "--record-fixture",
        ],
    )

    assert result.exit_code == 2
