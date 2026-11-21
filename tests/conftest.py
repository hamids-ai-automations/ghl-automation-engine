from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ghl_mock import MockState, serve_in_thread  # noqa: E402


class FakeClock:
    """Monotonic stand-in: sleep() advances time instead of blocking."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mock_server():
    state = MockState()
    server, base_url = serve_in_thread(state)
    try:
        yield state, base_url
    finally:
        server.shutdown()
        server.server_close()
