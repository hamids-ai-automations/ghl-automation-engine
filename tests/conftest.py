from __future__ import annotations

import sys
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


if sys.platform == "win32":
    # pytest points a "<prefix>current" directory symlink at the newest temp dir
    # and deletes the stale one during cleanup. On machines where deleting a
    # directory symlink is denied, that cleanup raises PermissionError and turns a
    # fully green run into a failure. Skipping the bookkeeping link is enough:
    # nothing under test reads it.
    from _pytest import pathlib as _pytest_pathlib
    from _pytest import tmpdir as _pytest_tmpdir

    def _skip_numbered_dir_symlink(*args: object, **kwargs: object) -> None:
        return None

    def _skip_dead_symlink_cleanup(*args: object, **kwargs: object) -> None:
        return None

    _pytest_pathlib._force_symlink = _skip_numbered_dir_symlink  # type: ignore[assignment]
    _pytest_pathlib.cleanup_dead_symlinks = _skip_dead_symlink_cleanup  # type: ignore[assignment]
    _pytest_tmpdir._force_symlink = _skip_numbered_dir_symlink  # type: ignore[assignment]
    _pytest_tmpdir.cleanup_dead_symlinks = _skip_dead_symlink_cleanup  # type: ignore[assignment]


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
