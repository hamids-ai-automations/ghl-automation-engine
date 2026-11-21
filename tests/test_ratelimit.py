from __future__ import annotations

import pytest

from ghl_client.ratelimit import SlidingWindowLimiter


def _limiter(clock, capacity: int = 90, window: float = 10.0) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(
        capacity=capacity, window=window, clock=clock, sleep=clock.sleep
    )


def test_burst_up_to_capacity_is_free(clock):
    limiter = _limiter(clock)
    waited = sum(limiter.acquire() for _ in range(90))

    assert waited == pytest.approx(0.0)
    assert limiter.remaining() == 0
    assert clock.sleeps == []


def test_request_beyond_capacity_waits_a_full_window(clock):
    limiter = _limiter(clock)
    for _ in range(90):
        limiter.acquire()

    waited = limiter.acquire()

    assert waited == pytest.approx(10.0)
    assert clock.sleeps == [pytest.approx(10.0)]
    assert limiter.used == 1  # only the new event remains inside the window


def test_window_slides_so_capacity_returns(clock):
    limiter = _limiter(clock, capacity=2)
    limiter.acquire()
    limiter.acquire()
    assert limiter.remaining() == 0

    clock.advance(10.1)

    assert limiter.remaining() == 2
    limiter.acquire()
    limiter.acquire()
    assert limiter.remaining() == 0


def test_default_budget_sits_below_platform_limit():
    limiter = SlidingWindowLimiter()
    assert limiter.capacity == 90  # documented platform limit is 100 per 10 s
    assert limiter.window == 10.0


def test_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        SlidingWindowLimiter(capacity=0)
    with pytest.raises(ValueError):
        SlidingWindowLimiter(window=0)
