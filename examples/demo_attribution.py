"""Offline attribution demo: two deliveries, one lead, credit intact.

Run: python examples/demo_attribution.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ghl_attribution import AttributionEngine  # noqa: E402
from ghl_client.auth import PrivateIntegrationToken  # noqa: E402
from ghl_client.client import GhlClient  # noqa: E402
from ghl_client.transport import UrllibTransport  # noqa: E402
from ghl_mock import DEFAULT_TOKEN, MockState, serve_in_thread  # noqa: E402


def main() -> int:
    state = MockState()
    server, base_url = serve_in_thread(state)
    try:
        client = GhlClient(
            PrivateIntegrationToken(DEFAULT_TOKEN),
            base_url=base_url,
            transport=UrllibTransport(),
            location_id="loc_demo",
        )
        engine = AttributionEngine(client)

        first = {
            "event_id": "evt-1001",
            "email": "ada@example.com",
            "first_name": "Ada",
            "utm_source": "google",
            "utm_medium": "cpc",
            "utm_campaign": "spring-launch",
            "occurred_at": "2026-11-20T09:15:00Z",
        }
        retry = dict(first)  # same event id: the relay delivered it twice
        later = {
            "event_id": "evt-1002",
            "email": "ada@example.com",
            "utm_source": "newsletter",
            "utm_medium": "email",
            "utm_campaign": "november-digest",
            "occurred_at": "2026-11-21T18:40:00Z",
        }
        direct = {
            "event_id": "evt-1003",
            "email": "ada@example.com",
            "utm_source": "direct",
            "occurred_at": "2026-11-22T07:05:00Z",
        }

        print("delivery 1 :", engine.ingest(first).to_dict())
        print("delivery 1':", engine.ingest(retry).to_dict())
        print("delivery 2 :", engine.ingest(later).to_dict())
        print("delivery 3 :", engine.ingest(direct).to_dict())

        contact = next(iter(state.store.all_contacts())).to_dict()
        fields = {item["id"]: item["value"] for item in contact["customFields"]}
        print("\ncontact:", contact["email"], contact["tags"])
        for key in sorted(fields):
            print(f"  {key}: {fields[key]}")

        assert state.store.created == 1, "expected exactly one contact"
        assert fields["cf_attribution_source"] == "google", "credit must survive a later touch"
        assert fields["cf_attribution_last_touch_at"] == "2026-11-21T18:40:00Z", "activity must advance"
        print("\nok: 1 contact, credit intact, last touch advanced, direct revisit ignored")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
