"""The mocked operator console.

The UI is the mock, so these tests are about the contract it exposes rather than the page:
what an operator is shown, and what pressing Resume actually does.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cua.escalation.intervention import (
    HumanAction,
    InterventionRequest,
    read_request,
    resume_signal,
    write_request,
)
from cua.escalation.operator_app import create_app
from cua.session.lock import LockState
from cua.surfaces.base import AXNode, Observation, PruningStats


def snapshot_file(directory: Path, tree: AXNode | None) -> Path:
    """An observation on disk, as `Handoff.escalate` writes one."""
    directory.mkdir(parents=True, exist_ok=True)
    observation = Observation(
        url="http://127.0.0.1:8099/tenant-a/members/12345",
        title="Member",
        tree=tree,
        observation_hash="h",
        pruning=PruningStats(nodes_before=9, nodes_after=3),
    )
    path = directory / "intervention-s1.ax.json"
    path.write_text(observation.model_dump_json(indent=2), encoding="utf-8")
    return path


DEFAULT_TREE = AXNode(
    role="RootWebArea", name="Member 12345", children=[AXNode(role="link", name="Open")]
)


def raise_intervention(
    root: Path,
    run_id: str = "run-1",
    *,
    blind: bool = False,
    resolved: bool = False,
) -> InterventionRequest:
    """`blind` means the run could see nothing at all, as it cannot inside a frameset."""
    directory = root / run_id
    path = snapshot_file(directory, None if blind else DEFAULT_TREE)
    request = InterventionRequest(
        run_id=run_id,
        reason="risky_action_blocked",
        step_id="s1",
        message="The policy refused a Close account click.",
        capability_id="member.savings_balance.read",
        capability_version="1.0.0",
        url="http://127.0.0.1:8099/tenant-a/members/12345",
        ax_snapshot_path=str(path),
        lock=LockState(session_id=run_id, holder="none"),
    )
    if resolved:
        from datetime import UTC, datetime

        request = request.model_copy(
            update={
                "resolved_at": datetime.now(UTC),
                "resumed_by": "operator",
                "human_actions": [HumanAction(kind="click", role="link", name="Open")],
            }
        )
    write_request(request, directory)
    return request


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


def items(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/interventions")
    assert response.status_code == 200
    payload: list[dict[str, Any]] = response.json()
    return payload


# ---- the page ----------------------------------------------------------------------------


def test_the_console_serves_one_static_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Operator console" in response.text
    assert "setInterval(refresh" in response.text, "the page polls rather than pushing"


def test_the_page_is_the_only_ui(client: TestClient) -> None:
    """Deliberately minimal: no docs, no second screen, no session list per operator."""
    assert client.get("/docs").status_code == 404


# ---- what an operator is shown ---------------------------------------------------------------


def test_an_empty_evidence_directory_shows_nothing_to_do(client: TestClient) -> None:
    assert items(client) == []


def test_a_pending_intervention_is_listed_with_what_the_operator_needs(
    client: TestClient, tmp_path: Path
) -> None:
    raise_intervention(tmp_path)

    listed = items(client)

    assert len(listed) == 1
    card = listed[0]
    assert card["reason"] == "risky_action_blocked"
    assert card["capability_id"] == "member.savings_balance.read"
    assert card["step_id"] == "s1"
    assert card["message"].startswith("The policy refused")
    assert card["url"].endswith("/members/12345")
    assert card["open"] is True


def test_the_ax_snapshot_is_rendered_as_the_run_saw_it(client: TestClient, tmp_path: Path) -> None:
    """The operator reads the same view the run was working from, not a screenshot."""
    raise_intervention(tmp_path)

    card = items(client)[0]

    assert '- RootWebArea "Member 12345"' in card["ax_tree"]
    assert '- link "Open"' in card["ax_tree"]


def test_a_run_that_could_see_nothing_says_so(client: TestClient, tmp_path: Path) -> None:
    """A frameset is exactly when a human is needed, so the console must not show a blank box."""
    raise_intervention(tmp_path, blind=True)

    assert "could see nothing at all" in items(client)[0]["ax_tree"]


def test_a_missing_snapshot_does_not_break_the_console(client: TestClient, tmp_path: Path) -> None:
    raise_intervention(tmp_path)
    (tmp_path / "run-1" / "intervention-s1.ax.json").unlink()

    assert "snapshot missing" in items(client)[0]["ax_tree"]


def test_a_resolved_intervention_shows_who_took_it(client: TestClient, tmp_path: Path) -> None:
    raise_intervention(tmp_path, resolved=True)

    card = items(client)[0]

    assert card["open"] is False
    assert card["resumed_by"] == "operator"
    assert card["human_action_count"] == 1


def test_open_interventions_come_before_resolved_ones(client: TestClient, tmp_path: Path) -> None:
    """An operator wants the work, not the history."""
    raise_intervention(tmp_path, "run-done", resolved=True)
    raise_intervention(tmp_path, "run-waiting")

    assert [card["run_id"] for card in items(client)] == ["run-waiting", "run-done"]


def test_one_intervention_can_be_fetched_directly(client: TestClient, tmp_path: Path) -> None:
    raise_intervention(tmp_path)
    assert client.get("/api/interventions/run-1").json()["step_id"] == "s1"


def test_asking_for_a_run_with_no_intervention_is_a_404(client: TestClient) -> None:
    assert client.get("/api/interventions/never-happened").status_code == 404


# ---- what the Resume button does ---------------------------------------------------------------


def test_resume_writes_a_signal_the_waiting_run_will_find(
    client: TestClient, tmp_path: Path
) -> None:
    raise_intervention(tmp_path)

    response = client.post("/api/interventions/run-1/resume", json={"by": "aditi"})

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "resumed_by": "aditi"}
    signal = resume_signal(tmp_path / "run-1")
    assert signal is not None
    assert signal.by == "aditi"
    assert signal.run_id == "run-1"


def test_resume_defaults_to_a_generic_operator(client: TestClient, tmp_path: Path) -> None:
    raise_intervention(tmp_path)
    client.post("/api/interventions/run-1/resume", json={})

    signal = resume_signal(tmp_path / "run-1")
    assert signal is not None and signal.by == "operator"


def test_the_console_never_touches_the_lock_itself(client: TestClient, tmp_path: Path) -> None:
    """It has no reference to the session. A console that could seize control silently
    would be a worse bug than the one this exists to solve."""
    raise_intervention(tmp_path)
    before = read_request(tmp_path / "run-1")

    client.post("/api/interventions/run-1/resume", json={"by": "aditi"})

    after = read_request(tmp_path / "run-1")
    assert before is not None and after is not None
    assert after.lock == before.lock
    assert after.open, "only the run may close its own intervention, after it resumes"


def test_resuming_a_run_with_no_intervention_is_a_404(client: TestClient) -> None:
    assert client.post("/api/interventions/nope/resume", json={}).status_code == 404


def test_the_signal_is_plain_json_anyone_can_read(client: TestClient, tmp_path: Path) -> None:
    raise_intervention(tmp_path)
    client.post("/api/interventions/run-1/resume", json={"by": "aditi"})

    written = json.loads((tmp_path / "run-1" / "resume.json").read_text(encoding="utf-8"))
    assert set(written) == {"run_id", "by", "at"}
