"""The 429 policy is the one piece of LLM plumbing that must not be wrong."""

import httpx
import pytest

from conftest import FakeClock, FakeTransport
from cua.llm.limiter import (
    BACKOFF_SECONDS,
    MAX_WAIT_SECONDS,
    QuotaExhaustedError,
    RateLimiter,
    classify_429,
    parse_duration,
)

# Real free-tier bodies, trimmed.
GEMINI_RPD = httpx.Response(
    429,
    json={
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                    ],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "27s"},
            ],
        }
    },
)
GEMINI_RPM = httpx.Response(
    429,
    json={
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "violations": [
                        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}
                    ]
                }
            ],
        }
    },
)
GROQ_RPD = httpx.Response(
    429,
    json={
        "error": {
            "message": (
                "Rate limit reached for model `llama-3.3-70b-versatile` on requests per day "
                "(RPD): Limit 14400, Used 14400. Please try again in 5m41s."
            ),
            "code": "rate_limit_exceeded",
        }
    },
)
GROQ_RPM = httpx.Response(
    429,
    json={
        "error": {
            "message": "Rate limit reached on requests per minute (RPM): Limit 30, Used 30.",
            "code": "rate_limit_exceeded",
        }
    },
)
AMBIGUOUS = httpx.Response(429, text="Too Many Requests")
OK = httpx.Response(200, json={"ok": True})


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (GEMINI_RPD, "daily"),
        (GEMINI_RPM, "minute"),
        (GROQ_RPD, "daily"),
        (GROQ_RPM, "minute"),
        (AMBIGUOUS, "ambiguous"),
    ],
)
def test_classify_429(response: httpx.Response, expected: str) -> None:
    assert classify_429(response.text) == expected


def test_daily_marker_wins_over_a_retry_hint() -> None:
    """A per-day body that also carries retryDelay is still fatal, not a 27s wait."""
    assert classify_429(GEMINI_RPD.text) == "daily"


def _limiter(clock: FakeClock, rpm: int = 12) -> RateLimiter:
    return RateLimiter(rpm, monotonic=clock.monotonic, sleep=clock.sleep)


def test_backoff_schedule_is_1_2_4_8() -> None:
    clock = FakeClock()
    transport = FakeTransport(GEMINI_RPM, GEMINI_RPM, GEMINI_RPM, OK)
    http = transport.client()

    response = _limiter(clock).call(lambda: http.post("https://x.test"), provider="gemini")

    assert response.status_code == 200
    assert clock.sleeps == [1.0, 2.0, 4.0]
    assert transport.calls == 4


def test_rpm_throttling_paces_calls() -> None:
    clock = FakeClock()
    transport = FakeTransport(OK)
    http = transport.client()
    limiter = _limiter(clock, rpm=12)

    for _ in range(12):
        limiter.call(lambda: http.post("https://x.test"), provider="gemini")
    assert clock.sleeps == []  # the bucket starts full

    limiter.call(lambda: http.post("https://x.test"), provider="gemini")
    assert clock.sleeps == [pytest.approx(5.0)]  # 60s / 12rpm
    assert clock.now == pytest.approx(5.0)


def test_rpd_raises_immediately_without_retrying() -> None:
    clock = FakeClock()
    transport = FakeTransport(GEMINI_RPD)
    http = transport.client()

    with pytest.raises(QuotaExhaustedError, match="daily free-tier quota"):
        _limiter(clock).call(lambda: http.post("https://x.test"), provider="gemini")

    assert transport.calls == 1
    assert clock.sleeps == []


def test_rpd_after_a_transient_429_still_stops_at_once() -> None:
    clock = FakeClock()
    transport = FakeTransport(GROQ_RPM, GROQ_RPD)
    http = transport.client()

    with pytest.raises(QuotaExhaustedError):
        _limiter(clock).call(lambda: http.post("https://x.test"), provider="groq")

    assert transport.calls == 2
    assert clock.sleeps == [1.0]


def test_ambiguous_429_exhausts_backoff_then_is_treated_as_rpd() -> None:
    clock = FakeClock()
    transport = FakeTransport(AMBIGUOUS)
    http = transport.client()

    with pytest.raises(QuotaExhaustedError, match="treating as daily quota"):
        _limiter(clock).call(lambda: http.post("https://x.test"), provider="gemini")

    assert clock.sleeps == list(BACKOFF_SECONDS)
    assert transport.calls == len(BACKOFF_SECONDS) + 1


def test_persistent_rpm_429_also_fails_the_run() -> None:
    clock = FakeClock()
    transport = FakeTransport(GEMINI_RPM)
    http = transport.client()

    with pytest.raises(QuotaExhaustedError, match="'minute'"):
        _limiter(clock).call(lambda: http.post("https://x.test"), provider="gemini")

    assert clock.sleeps == list(BACKOFF_SECONDS)


# --- provider retry hints: a real Groq run showed the fixed schedule undershooting -----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27.795s", 27.795),
        ("30", 30.0),
        ("2m30s", 150.0),
        ("500ms", 0.5),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_duration(raw: str, expected: float | None) -> None:
    assert parse_duration(raw) == (pytest.approx(expected) if expected is not None else None)


def test_a_token_reset_hint_beats_the_fixed_schedule() -> None:
    """Groq caps tokens per minute; 1s is never long enough to refill a 60s budget."""
    clock = FakeClock()
    tpm = httpx.Response(
        429,
        json={"error": {"message": "Rate limit reached on tokens per minute (TPM)"}},
        headers={"x-ratelimit-reset-tokens": "27.795s"},
    )
    transport = FakeTransport(tpm, OK)
    http = transport.client()

    _limiter(clock).call(lambda: http.post("https://x.test"), provider="groq")

    assert clock.sleeps == [pytest.approx(27.795)]


def test_a_shorter_hint_does_not_shorten_the_schedule() -> None:
    clock = FakeClock()
    transport = FakeTransport(
        httpx.Response(429, text="per minute", headers={"retry-after": "0.2"}), OK
    )
    http = transport.client()

    _limiter(clock).call(lambda: http.post("https://x.test"), provider="groq")

    assert clock.sleeps == [1.0]  # the floor still applies


def test_an_absurd_hint_is_capped() -> None:
    clock = FakeClock()
    transport = FakeTransport(
        httpx.Response(429, text="per minute", headers={"retry-after": "99999"}), OK
    )
    http = transport.client()

    _limiter(clock).call(lambda: http.post("https://x.test"), provider="groq")

    assert clock.sleeps == [MAX_WAIT_SECONDS]
