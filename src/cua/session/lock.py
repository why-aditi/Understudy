"""ControlLock: tracks whether automation, a human, or nobody currently drives a session.

Requirement 3.6 has a human take over the *same live session* an automation was driving. Two
parties sharing one browser need a rule about who is allowed to touch it, and the rule has to
be enforced rather than agreed: a run that keeps clicking while an operator is mid-correction
produces exactly the kind of interleaved mess nobody can debug afterwards.

So the lock is not advisory. `WebSurface.act` calls `require()` before every action, and an
automation that does not hold the lock cannot act at all.
"""

import logging
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel

_log = logging.getLogger(__name__)

Holder = Literal["automation", "human", "none"]


class LockError(Exception):
    """The lock is not in the state this operation needs."""


class LockState(BaseModel):
    """A serialisable snapshot, for the run log and for an intervention record."""

    session_id: str
    holder: Holder
    acquired_at: datetime | None = None
    acquired_by: str | None = None


class ControlLock:
    """Who is allowed to drive one session right now.

    Deliberately not re-entrant. Acquiring a held lock raises rather than nesting, because
    every case where that happens is a bug: two runs sharing a session, or a run that never
    released after handing control to a human.
    """

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self.holder: Holder = "none"
        self.acquired_at: datetime | None = None
        self.acquired_by: str | None = None

    # ponytail: not thread-safe. One run per session in-process; add a Lock if that changes.
    def acquire(self, holder: Holder, by: str) -> None:
        """Take control. Fails loudly if anyone already has it."""
        if holder == "none":
            raise LockError("cannot acquire a lock for holder 'none'; use release()")
        if self.holder != "none":
            raise LockError(
                f"session {self.session_id!r} is already held by {self.holder} "
                f"({self.acquired_by!r} since {self.acquired_at:%H:%M:%S}); "
                f"{holder} ({by!r}) must wait for it to be released"
            )
        self.holder = holder
        self.acquired_by = by
        self.acquired_at = datetime.now(UTC)
        _log.info(
            "lock_acquired",
            extra={"session_id": self.session_id, "holder": holder, "acquired_by": by},
        )

    def release(self) -> None:
        """Give control back. Releasing an unheld lock is fine; it is already the goal state."""
        if self.holder == "none":
            return
        _log.info(
            "lock_released",
            extra={
                "session_id": self.session_id,
                "holder": self.holder,
                "acquired_by": self.acquired_by,
            },
        )
        self.holder = "none"
        self.acquired_by = None
        self.acquired_at = None

    def require(self, holder: Holder) -> None:
        """Assert this party holds the lock. Called before every action on the surface."""
        if self.holder != holder:
            raise LockError(
                f"{holder} tried to act on session {self.session_id!r} but the lock is held "
                f"by {self.holder}" + (f" ({self.acquired_by!r})" if self.acquired_by else "")
            )

    def held_by(self, holder: Holder) -> bool:
        return self.holder == holder

    @property
    def held(self) -> bool:
        return self.holder != "none"

    def snapshot(self) -> LockState:
        return LockState(
            session_id=self.session_id,
            holder=self.holder,
            acquired_at=self.acquired_at,
            acquired_by=self.acquired_by,
        )

    @classmethod
    def for_automation(cls, session_id: str = "", by: str = "run") -> Self:
        """A lock already held by automation, for a run that owns its own session."""
        lock = cls(session_id)
        lock.acquire("automation", by)
        return lock

    def __repr__(self) -> str:
        return f"ControlLock(session={self.session_id!r}, holder={self.holder!r})"
