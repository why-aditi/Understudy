"""A surface that replays a recorded tape instead of driving a browser.

The deterministic half of this system does not need a model, and it turns out it does not
need a network either. Everything replay decides comes from the artifact and the
accessibility tree in front of it, so if the tree is on disk the whole path runs offline: no
Chromium, no API key, no socket. That makes the central claim checkable by a reviewer with
neither, which is the point of this module.

The tape is an ordered list of frames, one per `observe()` and one per `act()`, recorded in
the order the engine asked for them. Playback is strict: `act()` compares the action it is
given against the action recorded at that position and fails loudly if they differ. A lenient
fixture surface would accept whatever it was handed and always "pass", which would prove
nothing at all - the strictness is what makes an offline run a real regression test. If the
resolver picks a different control than it did at record time, this says so.
"""

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cua.surfaces.base import Action, ActionResult, Observation, Surface

_log = logging.getLogger(__name__)

FIXTURE_DIR = Path("fixtures")


class FixtureError(Exception):
    """The tape and the run disagree. Never a silent pass."""


class Frame(BaseModel):
    """One recorded interaction with a live surface, in the order it happened."""

    kind: Literal["observe", "act"] = Field(description="Which call produced this frame.")
    observation: Observation | None = Field(
        default=None, description="What observe() returned. Present on observe frames."
    )
    action: Action | None = Field(
        default=None, description="The action act() was given. Present on act frames."
    )
    result: ActionResult | None = Field(
        default=None, description="What act() returned. Present on act frames."
    )


class Fixture(BaseModel):
    """A recorded run, replayable with no browser and no network."""

    capability_id: str = Field(description="Capability this tape was recorded for.")
    capability_version: str = Field(description="Artifact version at record time.")
    tenant_id: str | None = Field(default=None, description="Tenant the recording ran against.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Parameters supplied at record time. A tape is parameter-specific: the values "
            "typed into the surface are baked into it."
        ),
    )
    entry_url: str = Field(description="Entry point the recording started from.")
    frames: list[Frame] = Field(description="Every observe and act, in order.")


def _shape(action: Action) -> tuple[Any, ...]:
    """The parts of an action that identify it, ignoring timing.

    `timeout_ms` is deliberately excluded: a different timeout is the same action.
    """
    target = action.target
    return (
        action.kind,
        action.value,
        target.role if target else None,
        target.name if target else None,
        target.near if target else None,
        target.nth if target else None,
        target.exact if target else None,
    )


def describe(action: Action) -> str:
    """One action in the form the mismatch message needs."""
    target = action.target
    where = ""
    if target:
        where = f" {target.role} {target.name!r}"
        if target.near:
            where += f" near={target.near!r}"
        if target.nth:
            where += f" nth={target.nth}"
    value = f" = {action.value!r}" if action.value is not None else ""
    return f"{action.kind}{where}{value}"


class FixtureSurface:
    """Plays a recorded tape back to the replay engine. Implements `Surface`."""

    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture
        self._position = 0

    def _next(self, kind: str) -> Frame:
        if self._position >= len(self.fixture.frames):
            raise FixtureError(
                f"the tape ran out: the run asked for {kind!r} after "
                f"{len(self.fixture.frames)} frames. This recording is for a shorter run."
            )
        frame = self.fixture.frames[self._position]
        if frame.kind != kind:
            raise FixtureError(
                f"frame {self._position} is {frame.kind!r} but the run asked for {kind!r}. "
                "The run is taking a different path than the one recorded."
            )
        self._position += 1
        return frame

    def observe(self, *, screenshot: bool = False) -> Observation:
        """The recorded observation at this position.

        `screenshot` is accepted and ignored: a tape carries an accessibility tree, never
        pixels. Offline evidence is the tree, which is what failure capture uses anyway.
        """
        frame = self._next("observe")
        if frame.observation is None:  # pragma: no cover - the model guarantees this pairing
            raise FixtureError(f"frame {self._position - 1} is an observe frame with no tree")
        return frame.observation

    def act(self, action: Action) -> ActionResult:
        """The recorded result, but only if this is the action that was recorded here."""
        frame = self._next("act")
        if frame.action is None or frame.result is None:  # pragma: no cover - model guarantee
            raise FixtureError(f"frame {self._position - 1} is an act frame with no action")
        if _shape(action) != _shape(frame.action):
            raise FixtureError(
                f"frame {self._position - 1}: the recording did "
                f"[{describe(frame.action)}] but this run tried [{describe(action)}]. "
                "Replay resolved a different control than it did at record time."
            )
        return frame.result

    @property
    def exhausted(self) -> bool:
        """Whether every recorded frame was consumed."""
        return self._position >= len(self.fixture.frames)


class RecordingSurface:
    """Wraps a live surface and writes down everything it was asked. Implements `Surface`."""

    def __init__(self, inner: Surface) -> None:
        self._inner = inner
        self.frames: list[Frame] = []

    def observe(self, *, screenshot: bool = False) -> Observation:
        observation = self._inner.observe(screenshot=screenshot)
        # Screenshots are excluded from the model dump, so a tape never carries pixels even
        # when the run that produced it was capturing them.
        self.frames.append(Frame(kind="observe", observation=observation))
        return observation

    def act(self, action: Action) -> ActionResult:
        result = self._inner.act(action)
        self.frames.append(Frame(kind="act", action=action, result=result))
        return result


# ---- on disk --------------------------------------------------------------------------------


def fixture_path(capability_id: str, directory: Path = FIXTURE_DIR) -> Path:
    return directory / f"{capability_id}.fixture.json"


def save_fixture(fixture: Fixture, directory: Path = FIXTURE_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = fixture_path(fixture.capability_id, directory)
    path.write_text(fixture.model_dump_json(indent=2), encoding="utf-8")
    _log.info(
        "fixture_saved",
        extra={"path": str(path), "frames": len(fixture.frames)},
    )
    return path


def load_fixture(capability_id: str, directory: Path = FIXTURE_DIR) -> Fixture:
    """Read a recorded tape. Reading a file is the only I/O an offline replay performs."""
    path = fixture_path(capability_id, directory)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureError(
            f"no offline fixture for {capability_id!r} at {path}: {exc}. "
            "Record one with `cua replay --capability "
            f"{capability_id} --record-fixture`."
        ) from exc
    try:
        return Fixture.model_validate(json.loads(raw))
    except (ValidationError, ValueError) as exc:
        raise FixtureError(f"fixture at {path} is not a valid recording: {exc}") from exc
