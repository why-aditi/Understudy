"""The loop: one action per turn, every action gated, and a stopping condition that fires."""

import json
from pathlib import Path

import pytest

from cua.discovery.runner import (
    NO_CALL_NUDGE,
    DiscoveryConfig,
    DiscoveryError,
    DiscoveryRunner,
    new_run_id,
)
from cua.discovery.tools import TOOL_NAMES, TOOL_SPECS, ClosedSchemaViolation, parse
from cua.evidence.logger import RunLogger
from cua.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from cua.policy.engine import PolicyEngine
from cua.policy.rules import load_policy
from cua.surfaces.base import Action, ActionResult, AXNode, Observation
from cua.surfaces.pruning import observation_hash, prune

REPO = Path(__file__).resolve().parents[1]
TARGET = "http://localhost:8080/members"


def page(url: str, *names: str) -> tuple[str, AXNode]:
    children = [AXNode(role="button", name=name) for name in names]
    return url, AXNode(role="RootWebArea", name="Members", children=children)


class FakeSurface:
    """Advances to the next scripted page on every successful action."""

    def __init__(self, states: list[tuple[str, AXNode]], *, advance: bool = True) -> None:
        self.states = states
        self.advance = advance
        self.index = 0
        self.acted: list[Action] = []

    def _state(self) -> tuple[str, AXNode]:
        return self.states[min(self.index, len(self.states) - 1)]

    def observe(self, *, screenshot: bool = False) -> Observation:
        url, tree = self._state()
        pruned, stats = prune(tree)
        return Observation(
            url=url,
            title="Members",
            tree=pruned,
            observation_hash=observation_hash(url, pruned),
            pruning=stats,
            screenshot=b"\x89PNG" if screenshot else None,
        )

    def act(self, action: Action) -> ActionResult:
        self.acted.append(action)
        # Navigating to the entry point lands you on the first scripted page, it does not
        # move past it. Advancing here made every scripted click get recorded against the
        # tree of the *next* page - harmless until something re-derived the target from
        # that tree, which the capability assembler now does.
        if self.advance and action.kind != "navigate":
            self.index += 1
        return ActionResult(action=action, ok=True, url_after=self._state()[0], duration_ms=1)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class FakeLLM:
    """Replays scripted responses, repeating the last one once exhausted."""

    supports_images = False

    def __init__(
        self, responses: list[LLMResponse], *, clock: Clock | None = None, cost_seconds: float = 0.0
    ) -> None:
        self.responses = responses
        self.calls: list[list[Message]] = []
        self.images_seen: list[list[bytes]] = []
        self._clock = clock
        self._cost = cost_seconds

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        images: list[bytes] | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        if images is not None:
            self.images_seen.append(images)
        if self._clock is not None:
            self._clock.now += self._cost
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def says(text: str, tool: str, /, **arguments: object) -> LLMResponse:
    """One model turn: a sentence of reasoning plus exactly one tool call."""
    return LLMResponse(
        model="fake", text=text, tool_calls=[ToolCall(name=tool, arguments=arguments)]
    )


def build(
    surface: FakeSurface,
    llm: FakeLLM,
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    **overrides: object,
) -> tuple[DiscoveryRunner, RunLogger]:
    config = DiscoveryConfig.model_validate(
        {"goal": "read the balance", "target": TARGET, **overrides}
    )
    logger = RunLogger(new_run_id(), root=tmp_path)
    runner = DiscoveryRunner(
        surface=surface,
        llm=llm,
        policy=PolicyEngine(load_policy(REPO / "policy.yaml")),
        logger=logger,
        config=config,
        monotonic=clock.monotonic if clock else __import__("time").monotonic,
    )
    return runner, logger


def records(logger: RunLogger) -> list[dict[str, object]]:
    return [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]


# ---- the happy path -------------------------------------------------------------------


def test_a_goal_is_reached_and_every_step_is_recorded(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search"), page(TARGET + "/12345", "Back")])
    llm = FakeLLM(
        [
            says("I will open the member", "click", role="button", name="Search"),
            says("Balance is on screen", "finish", summary="read it", outputs={"balance": "12.34"}),
        ]
    )
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        result = runner.run()

    assert result.stop_reason == "goal_reached"
    assert result.steps == 2
    assert result.outputs == {"balance": "12.34"}

    # The policy check precedes the act it gates, and finish never reaches the surface.
    events = [r["event"] for r in records(logger)]
    assert events == [
        "discovery_start",
        "policy_check",
        "discovery_entry",
        "policy_check",
        "discovery_step",
        "discovery_step",
        # The trace declares an output nothing extracted, so the assembler says so rather
        # than wiring it to an arbitrary step; the draft is still emitted.
        "assemble_output_unmatched",
        "capability_emitted",
        "discovery_end",
    ]


def test_each_step_record_carries_the_required_fields(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search"), page(TARGET + "/12345", "Back")])
    llm = FakeLLM(
        [
            says("clicking", "click", role="button", name="Search"),
            says("done", "finish", summary="ok"),
        ]
    )
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        runner.run()

    step = next(r for r in records(logger) if r["event"] == "discovery_step")
    assert step["step"] == 1
    assert isinstance(step["observation_hash"], str)
    assert step["pruning_ratio"] == 0.0
    assert step["reasoning"] == "clicking"
    assert step["action"] == {
        "kind": "click",
        "target": {"role": "button", "name": "Search", "nth": 0, "exact": True, "near": None},
        "value": None,
        "timeout_ms": 10000,
    }
    assert step["verdict"] is not None
    assert step["result"] is not None
    assert isinstance(step["elapsed_ms"], int)


def test_the_entry_point_is_opened_through_the_policy_check(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search")])
    llm = FakeLLM([says("done", "finish", summary="ok")])
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        runner.run()

    assert surface.acted[0] == Action(kind="navigate", value=TARGET)
    entry = next(r for r in records(logger) if r["event"] == "discovery_entry")
    assert entry["verdict"] is not None


def test_an_entry_point_outside_the_allowlist_never_starts(tmp_path: Path) -> None:
    surface = FakeSurface([page("https://example.com/", "Search")])
    runner, logger = build(surface, FakeLLM([]), tmp_path, target="https://example.com/")

    with logger, pytest.raises(DiscoveryError, match="policy refused the entry point"):
        runner.run()

    assert surface.acted == []


# ---- the closed schema is closed ------------------------------------------------------


def test_the_shipped_tool_schema_is_the_whole_vocabulary() -> None:
    assert TOOL_NAMES == {
        "navigate",
        "click",
        "type_text",
        "select_option",
        "press_key",
        "wait_for",
        "extract",
        "finish",
    }


def test_an_invented_tool_is_a_hard_error(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search")])
    llm = FakeLLM([says("improvising", "execute_javascript", script="alert(1)")])
    runner, logger = build(surface, llm, tmp_path)

    with logger, pytest.raises(ClosedSchemaViolation, match="unknown tool"):
        runner.run()


def test_arguments_outside_the_schema_are_a_hard_error(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search")])
    llm = FakeLLM([says("guessing", "click", selector="#search-btn")])
    runner, logger = build(surface, llm, tmp_path)

    with logger, pytest.raises(ClosedSchemaViolation, match="outside the schema"):
        runner.run()


@pytest.mark.parametrize("count", [0, 2])
def test_anything_but_exactly_one_action_per_turn_is_a_hard_error(
    tmp_path: Path, count: int
) -> None:
    calls = [ToolCall(name="click", arguments={"role": "button"})] * count
    llm = FakeLLM([LLMResponse(model="fake", text="thinking out loud", tool_calls=calls)])
    runner, logger = build(FakeSurface([page(TARGET, "Search")]), llm, tmp_path)

    with logger, pytest.raises(ClosedSchemaViolation, match="exactly one tool call"):
        runner.run()


# ---- stopping conditions --------------------------------------------------------------


def test_three_identical_observations_stop_the_run(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search")], advance=False)
    llm = FakeLLM([says("trying again", "click", role="button", name="Search")])
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        result = runner.run()

    assert result.stop_reason == "no_progress"
    assert result.steps == 2  # the third identical observation stops it before it acts again
    assert any(r["event"] == "discovery_no_progress" for r in records(logger))


def test_the_step_ceiling_stops_the_run(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, f"Row {i}") for i in range(10)])
    llm = FakeLLM([says("next", "click", role="button", name="Search")])
    runner, logger = build(surface, llm, tmp_path, max_steps=3)

    with logger:
        result = runner.run()

    assert (result.stop_reason, result.steps) == ("max_steps", 3)


def test_the_wall_clock_stops_the_run(tmp_path: Path) -> None:
    clock = Clock()
    surface = FakeSurface([page(TARGET, f"Row {i}") for i in range(10)])
    llm = FakeLLM(
        [says("slow", "click", role="button", name="Search")], clock=clock, cost_seconds=200.0
    )
    runner, logger = build(surface, llm, tmp_path, clock=clock, wall_clock_seconds=300.0)

    with logger:
        result = runner.run()

    assert (result.stop_reason, result.steps) == ("timeout", 2)


def test_an_irreversible_action_stops_the_run_before_it_happens(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Delete member")])
    llm = FakeLLM([says("cleaning up", "click", role="button", name="Delete member")])
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        result = runner.run()

    assert result.stop_reason == "escalation_required"
    assert surface.acted == [Action(kind="navigate", value=TARGET)]  # the delete never ran

    step = next(r for r in records(logger) if r["event"] == "discovery_step")
    assert step["result"] is None
    verdict = step["verdict"]
    assert isinstance(verdict, dict)
    assert verdict["decision"] == "block_and_escalate"


def test_a_refused_action_is_fed_back_and_the_loop_continues(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search"), page(TARGET, "Search")])
    llm = FakeLLM(
        [
            says("looking elsewhere", "navigate", url="https://example.com/search"),
            says("staying put", "finish", summary="gave up on the detour"),
        ]
    )
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        result = runner.run()

    assert result.stop_reason == "goal_reached"
    assert surface.acted == [Action(kind="navigate", value=TARGET)]  # the detour never ran

    # The refusal is fed back as the turn before the next observation.
    contents = [message.content for message in llm.calls[-1]]
    assert any("safety policy refused" in content for content in contents)


# ---- one nudge for a turn with no tool call ------------------------------------------


def no_call(text: str) -> LLMResponse:
    """The model reasoned and forgot to act."""
    return LLMResponse(model="fake", text=text, tool_calls=[])


def test_a_turn_with_no_tool_call_gets_one_nudge(tmp_path: Path) -> None:
    surface = FakeSurface([page(TARGET, "Search")])
    llm = FakeLLM(
        [
            no_call("The balance is 4,182.55, so we are done."),
            says(
                "calling finish now", "finish", summary="read it", outputs={"balance": "4,182.55"}
            ),
        ]
    )
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        result = runner.run()

    assert result.stop_reason == "goal_reached"
    assert result.outputs == {"balance": "4,182.55"}
    assert len(llm.calls) == 2  # the nudge costs one extra call, not one extra step
    assert result.steps == 1

    nudge = next(r for r in records(logger) if r["event"] == "discovery_nudge")
    assert nudge["said"] == "The balance is 4,182.55, so we are done."
    assert any(NO_CALL_NUDGE in m.content for m in llm.calls[-1])


def test_a_second_empty_turn_is_still_a_hard_error(tmp_path: Path) -> None:
    """One nudge, not a conversation."""
    llm = FakeLLM([no_call("still thinking")])
    runner, logger = build(FakeSurface([page(TARGET, "Search")]), llm, tmp_path)

    with logger, pytest.raises(ClosedSchemaViolation, match="exactly one tool call"):
        runner.run()

    assert len(llm.calls) == 2


def test_an_invented_tool_is_never_nudged(tmp_path: Path) -> None:
    """Forgetting to act is a lapse; inventing a tool is a challenge to the schema."""
    llm = FakeLLM([says("improvising", "run_sql", query="select 1")])
    runner, logger = build(FakeSurface([page(TARGET, "Search")]), llm, tmp_path)

    with logger, pytest.raises(ClosedSchemaViolation, match="unknown tool"):
        runner.run()

    assert len(llm.calls) == 1  # no second chance


# ---- screenshots: capture and vision are separate decisions ---------------------------


def test_screenshots_are_captured_as_evidence_even_for_a_text_only_model(tmp_path: Path) -> None:
    llm = FakeLLM([says("done", "finish", summary="ok")])  # supports_images = False
    runner, logger = build(FakeSurface([page(TARGET, "Search")]), llm, tmp_path, screenshots=True)

    with logger:
        runner.run()

    shot = next(r for r in records(logger) if r["event"] == "screenshot_captured")
    assert shot["sent_to_model"] is False
    assert (logger.directory / "screenshots" / "step-01.png").read_bytes() == b"\x89PNG"


def test_a_vision_model_is_sent_the_screenshot(tmp_path: Path) -> None:
    llm = FakeLLM([says("done", "finish", summary="ok")])
    llm.supports_images = True
    runner, logger = build(FakeSurface([page(TARGET, "Search")]), llm, tmp_path, screenshots=True)

    with logger:
        runner.run()

    assert next(r for r in records(logger) if r["event"] == "screenshot_captured")["sent_to_model"]
    assert llm.images_seen == [[b"\x89PNG"]]


def test_no_screenshot_is_written_when_capture_is_off(tmp_path: Path) -> None:
    llm = FakeLLM([says("done", "finish", summary="ok")])
    runner, logger = build(FakeSurface([page(TARGET, "Search")]), llm, tmp_path)

    with logger:
        runner.run()

    assert not any(r["event"] == "screenshot_captured" for r in records(logger))
    assert list(logger.directory.glob("*.png")) == []


def test_optional_target_fields_are_declared_nullable() -> None:
    """A model asked for "no name" emits null, and providers validate what they were sent.

    Declaring these as plain strings turned a reasonable model output into an HTTP 400 from
    Groq's own tool-call validator, which the loop never gets to see. Found that way.
    """
    click = next(spec for spec in TOOL_SPECS if spec.name == "click")
    properties = click.parameters["properties"]

    assert properties["name"]["type"] == ["string", "null"]
    assert properties["near"]["type"] == ["string", "null"]
    assert properties["role"]["type"] == "string", "a required field stays a plain string"
    assert "anyOf" not in properties["name"], "collapsed, because one provider rejects anyOf"


def test_a_null_name_parses_into_a_target_with_no_name() -> None:
    action = parse(ToolCall(name="click", arguments={"role": "link", "name": None, "near": None}))

    assert isinstance(action, Action)
    assert action.target is not None
    assert action.target.name is None
    assert action.target.near is None


def test_a_model_chosen_target_matches_the_name_exactly() -> None:
    """Substring matching silently picks the wrong control when labels overlap.

    On the harness search screen "Search" is a substring of the "Member search" nav link,
    which points at the same page: the click reports ok, nothing moves, and the run stalls
    with no error to explain it. Found on a live run.
    """
    action = parse(ToolCall(name="click", arguments={"role": "link", "name": "Search"}))

    assert isinstance(action, Action)
    assert action.target is not None
    assert action.target.exact is True


def test_repeated_reads_do_not_count_as_being_stuck(tmp_path: Path) -> None:
    """`extract` is a read: it leaves the page where it was, on purpose.

    Counting its unchanged observation made any capability that reads two values off one
    screen undiscoverable - the loop stopped for no progress before it could finish. Found
    on a live run that read the same results row twice.
    """
    surface = FakeSurface([page(TARGET, "Search")], advance=False)
    llm = FakeLLM(
        [
            says("read one", "extract", role="button", name="Search"),
            says("read two", "extract", role="button", name="Search"),
            says("read three", "extract", role="button", name="Search"),
            says("done", "finish", summary="read them all"),
        ]
    )
    runner, logger = build(surface, llm, tmp_path)

    with logger:
        result = runner.run()

    assert result.stop_reason == "goal_reached"
    assert result.steps == 4
