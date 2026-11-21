from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from ghl_client.errors import (
    GhlRateLimitError,
    GhlServerError,
    GhlValidationError,
)
from ghl_client.retry import (
    DEFAULT_MAX_DELAY,
    RetryPolicy,
    call_with_retry,
    parse_retry_after,
)


# -- Retry-After parsing ----------------------------------------------------


def test_parse_retry_after_seconds():
    assert parse_retry_after("120") == pytest.approx(120.0)
    assert parse_retry_after("0") == pytest.approx(0.0)
    assert parse_retry_after("  7 ") == pytest.approx(7.0)


def test_parse_retry_after_http_date():
    """The HTTP-date form is legal per RFC 9110 and breaks naive int parsing."""

    when = datetime.now(timezone.utc) + timedelta(seconds=60)
    parsed = parse_retry_after(format_datetime(when, usegmt=True))

    assert parsed is not None
    assert 55.0 <= parsed <= 61.0


def test_parse_retry_after_rejects_garbage():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None


# -- delay computation ------------------------------------------------------


def test_exponential_backoff_without_jitter():
    policy = RetryPolicy(base_delay=0.5, cap_delay=4.0, jitter=False)
    assert [policy.delay_for(a) for a in (1, 2, 3, 4, 5)] == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_retry_after_is_honoured_but_capped():
    policy = RetryPolicy()
    assert policy.delay_for(1, retry_after=3.0) == pytest.approx(3.0)

    huge = policy.delay_for(1, retry_after=3600.0)
    assert huge == pytest.approx(DEFAULT_MAX_DELAY)
    assert huge < 3600.0  # never freeze the caller for an hour


# -- the retry loop ---------------------------------------------------------


def _rate_limited(retry_after: float | None = None) -> GhlRateLimitError:
    return GhlRateLimitError("rate limited", status=429, retry_after=retry_after)


def test_retries_rate_limit_then_succeeds(clock):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _rate_limited()
        return "ok"

    events = []
    result = call_with_retry(
        call,
        policy=RetryPolicy(base_delay=1.0, jitter=False),
        sleep=clock.sleep,
        on_retry=events.append,
    )

    assert result == "ok"
    assert attempts["n"] == 3
    assert [e.attempt for e in events] == [1, 2]
    assert [e.delay for e in events] == [1.0, 2.0]


def test_non_retryable_error_is_raised_immediately(clock):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise GhlValidationError("bad payload", status=422)

    with pytest.raises(GhlValidationError):
        call_with_retry(
            call, policy=RetryPolicy(), sleep=clock.sleep
        )
    assert attempts["n"] == 1
    assert clock.sleeps == []


def test_server_error_is_retried_up_to_max_attempts(clock):
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise GhlServerError("boom", status=503)

    with pytest.raises(GhlServerError):
        call_with_retry(
            call,
            policy=RetryPolicy(max_attempts=4, base_delay=0.1, jitter=False),
            sleep=clock.sleep,
        )
    assert attempts["n"] == 4


def test_truncated_retry_after_is_surfaced(clock):
    attempts = {"n": 0}
    events = []

    def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _rate_limited(retry_after=3600.0)
        return "ok"

    call_with_retry(
        call,
        policy=RetryPolicy(),
        sleep=clock.sleep,
        on_retry=events.append,
    )

    assert events[0].truncated is True
    assert clock.sleeps[0] == pytest.approx(DEFAULT_MAX_DELAY)
