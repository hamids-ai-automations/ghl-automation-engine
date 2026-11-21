"""End-to-end demo against the offline mock. No credentials, no network.

Run: python examples/demo_offline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ghl_client import (  # noqa: E402
    ContactsResource,
    GhlClient,
    GhlPartialResultError,
    PrivateIntegrationToken,
    RetryPolicy,
    SlidingWindowLimiter,
    UrllibTransport,
)
from ghl_mock import Fault, MockState, serve_in_thread  # noqa: E402


def main() -> int:
    state = MockState()
    server, base_url = serve_in_thread(state)
    print(f"mock server: {base_url}")

    client = GhlClient(
        PrivateIntegrationToken(token=state.token),
        base_url=base_url,
        transport=UrllibTransport(),
        limiter=SlidingWindowLimiter(capacity=10_000, window=1.0),
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=False),
        location_id="loc_demo",
    )
    contacts = ContactsResource(client, page_size=10)

    try:
        print("\n1. seed 25 contacts (one duplicate to prove dedupe)")
        for i in range(24):
            contacts.upsert({"email": f"user{i}@example.com", "firstName": f"U{i}"})
        contacts.upsert({"email": "USER0@example.com"}, idempotency_key="dup")
        print(f"   provider created={state.store.created} updated={state.store.updated}")

        print("\n2. replay one upsert with the same key")
        before = len(state.requests)
        contacts.upsert({"email": "user1@example.com"}, idempotency_key="replay")
        contacts.upsert({"email": "user1@example.com"}, idempotency_key="replay")
        print(f"   http calls made: {len(state.requests) - before} (second was served from the ledger)")

        print("\n3. read every page")
        result = contacts.search()
        print(f"   contacts={len(result)} pages={result.pages_fetched} complete={result.complete}")

        print("\n4. inject a 429 with Retry-After, confirm the client survives")
        state.queue_fault("/contacts/search", Fault(status=429, times=1, retry_after="0"))
        retried = contacts.search()
        print(f"   complete={retried.complete} contacts={len(retried)}")

        print("\n5. fail in the middle of pagination and refuse to lie")
        state.queue_fault("/contacts/search", Fault(status=500, times=5, skip=1))
        try:
            contacts.search()
        except GhlPartialResultError as exc:
            print(f"   raised GhlPartialResultError collected={len(exc.collected)}")
            print("   -> an automation checking .complete can never archive the missing 15")
    finally:
        server.shutdown()
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
