"""Shared test doubles: a fake clock and a scripted httpx transport."""

import httpx


class FakeClock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Serves scripted responses in order, repeating the last once exhausted."""

    def __init__(self, *responses: httpx.Response) -> None:
        if not responses:
            raise ValueError("at least one response is required")
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    @property
    def calls(self) -> int:
        return len(self.requests)
