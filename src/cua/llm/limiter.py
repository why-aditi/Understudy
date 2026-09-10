"""Token-bucket rate limiter with exponential backoff and RPM-versus-RPD 429 discrimination."""

import logging
import re
import time
from collections.abc import Callable
from typing import Literal

import httpx

# Backoff schedule applied to transient 429s. Four sleeps => five attempts total.
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

# Ceiling on any single wait, however long the provider claims its reset is.
MAX_WAIT_SECONDS = 60.0

# Headers carrying "come back in N seconds", in the order we trust them.
_RETRY_HEADERS = ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests")

_log = logging.getLogger(__name__)

Quota = Literal["daily", "minute", "ambiguous"]

# Free tiers report exhaustion in prose, not in a machine-readable field, so both
# providers are matched by substring against the raw body.
#   Gemini: "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
#   Groq:   "Rate limit reached ... on requests per day (RPD): Limit 14400, Used 14400"
_DAILY = re.compile(r"per[\s_-]*day|\brpd\b|requests_per_day|daily|per[\s_-]*24[\s_-]*h", re.I)
_MINUTE = re.compile(
    r"per[\s_-]*minute|\brpm\b|\btpm\b|requests_per_minute|tokens_per_minute", re.I
)


class LLMError(Exception):
    """Any failure talking to a provider."""


class QuotaExhaustedError(LLMError):
    """The daily quota is gone. It will not clear for hours, so the run must stop."""


def classify_429(body: str) -> Quota:
    """Decide whether a 429 body describes a daily or a per-minute exhaustion.

    Daily wins when both markers are present: a body naming a per-day quota alongside a
    retry hint is still a per-day exhaustion, and retrying it burns the rest of the run.
    """
    if _DAILY.search(body):
        return "daily"
    if _MINUTE.search(body):
        return "minute"
    return "ambiguous"


def parse_duration(raw: str) -> float | None:
    """Seconds from a provider's retry hint: "27.8s", "2m30s", or a bare "30"."""
    text = raw.strip().lower()
    if not text:
        return None
    # Milliseconds first, or the minutes group below swallows the "m" of "ms".
    if text.endswith("ms"):
        head = re.fullmatch(r"\d+(?:\.\d+)?", text[:-2])
        return float(head.group()) / 1000.0 if head else None

    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?", text)
    if match is None or not any(match.groups()):
        return None
    minutes = float(match.group(1)) if match.group(1) else 0.0
    seconds = float(match.group(2)) if match.group(2) else 0.0
    return minutes * 60.0 + seconds


def retry_hint(response: httpx.Response) -> float | None:
    """The provider's own "wait this long", if it sent one."""
    for header in _RETRY_HEADERS:
        raw = response.headers.get(header)
        if raw:
            parsed = parse_duration(raw)
            if parsed is not None and parsed > 0:
                return parsed
    return None


class TokenBucket:
    """Paces calls to `rpm` per minute, starting with a full bucket.

    `monotonic` and `sleep` are injectable so tests can drive a fake clock.
    """

    # ponytail: not thread-safe. One discovery loop per process; add a Lock if that changes.
    def __init__(
        self,
        rpm: int = 12,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.rpm = rpm
        self._capacity = float(rpm)
        self._per_second = rpm / 60.0
        self._tokens = float(rpm)
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated = monotonic()

    def _refill(self) -> None:
        now = self._monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._per_second)
        self._updated = now

    def acquire(self) -> float:
        """Consume one token, sleeping if none is available. Returns seconds waited."""
        self._refill()
        waited = 0.0
        if self._tokens < 1.0 - 1e-9:
            waited = (1.0 - self._tokens) / self._per_second
            self._sleep(waited)
            self._refill()
        self._tokens = max(0.0, self._tokens - 1.0)
        return waited


class RateLimiter:
    """Wraps every provider call: paces it, then retries transient 429s with backoff."""

    def __init__(
        self,
        rpm: int = 12,
        *,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bucket = TokenBucket(rpm, monotonic=monotonic, sleep=sleep)
        self.backoff = backoff
        self.max_wait_seconds = max_wait_seconds
        self._sleep = sleep

    def call(self, send: Callable[[], httpx.Response], *, provider: str) -> httpx.Response:
        """Send a request, honouring the bucket and the 429 policy.

        Raises `QuotaExhaustedError` immediately on a daily-quota 429, and also when
        transient 429s survive the full backoff schedule - an ambiguous body that keeps
        returning 429 after the whole schedule is a daily exhaustion in every case we
        can act on.
        """
        last: Quota = "ambiguous"
        waited = 0.0
        for attempt in range(len(self.backoff) + 1):
            self.bucket.acquire()
            try:
                response = send()
            except httpx.HTTPError as exc:
                raise LLMError(f"{provider}: transport error: {exc}") from exc

            if response.status_code != 429:
                return response

            last = classify_429(response.text)
            if last == "daily":
                raise QuotaExhaustedError(
                    f"{provider}: daily free-tier quota (RPD) exhausted; it will not clear for "
                    f"hours. Stop the run and resume after the quota resets. "
                    f"Provider said: {response.text.strip()[:300]}"
                )
            if attempt < len(self.backoff):
                hint = retry_hint(response)
                delay = self._delay(response, self.backoff[attempt])
                waited += delay
                _log.info(
                    "rate_limited",
                    extra={
                        "provider": provider,
                        "attempt": attempt + 1,
                        "classified": last,
                        "delay_seconds": round(delay, 3),
                        "provider_hint_seconds": round(hint, 3) if hint else None,
                    },
                )
                self._sleep(delay)

        raise QuotaExhaustedError(
            f"{provider}: still rate-limited after {len(self.backoff)} retries over {waited:g}s "
            f"(last 429 classified as {last!r}); treating as daily quota (RPD) exhaustion."
        )

    def _delay(self, response: httpx.Response, floor: float) -> float:
        """The fixed schedule is a floor; the provider's own hint wins when it is longer.

        A per-minute token budget does not refill on our schedule. Groq reports 8000 TPM
        with a ~28s reset, so a fixed 1/2/4/8 can never outlast one window and every retry
        is spent for nothing. The hint is capped so a nonsense value cannot hang a run.
        """
        hint = retry_hint(response)
        if hint is None:
            return floor
        return min(max(floor, hint), self.max_wait_seconds)
