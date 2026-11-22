from __future__ import annotations

import getpass
import os
import sys
import tempfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ghl_mock import MockState, serve_in_thread


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


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Repair a dangling ``pytest-current`` link before pytest cleans up itself.

    On Windows ``os.unlink()`` on a directory symlink whose target is gone raises
    ``PermissionError``, and pytest's own teardown makes exactly that call, so a
    run where every test passed still exits non-zero. ``os.rmdir`` removes the
    link itself, which is all this needs.
    """

    root = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"
    current = root / "pytest-current"
    try:
        if current.is_symlink() and not current.exists():
            os.rmdir(current)
    except OSError:
        pass


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


@pytest.fixture
def mock_client(mock_server):
    """A real client pointed at the real mock socket, authenticated with the mock token."""

    from ghl_client.auth import PrivateIntegrationToken
    from ghl_client.client import GhlClient
    from ghl_client.ratelimit import SlidingWindowLimiter
    from ghl_client.retry import RetryPolicy
    from ghl_client.transport import UrllibTransport
    from ghl_mock import DEFAULT_TOKEN

    state, base_url = mock_server
    client = GhlClient(
        PrivateIntegrationToken(token=DEFAULT_TOKEN),
        base_url=base_url,
        transport=UrllibTransport(),
        limiter=SlidingWindowLimiter(capacity=10_000, window=1.0),
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.0, jitter=False),
        location_id="loc_test",
    )
    return state, client

