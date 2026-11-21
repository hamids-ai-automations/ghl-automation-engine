"""Offline webhook intake demo. No network, no credentials.

Run it:

    python examples/demo_webhooks.py

It walks a single delivery through every outcome the receiver can produce, using a
local clock so the output is stable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ghl_webhooks import Receiver, ReplayGuard  # noqa: E402
from ghl_webhooks.receiver import adapter_response  # noqa: E402

SECRET = "whsec_demo_only"
NOW = 1_800_000_000.0


def build_body(event_id: str, event_type: str = "ContactCreate") -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "locationId": "loc_demo",
            "timestamp": str(int(NOW)),
            "data": {"contactId": "c_1", "email": "lead@example.com"},
        }
    ).encode()


def signed(body: bytes, timestamp: float = NOW, secret: str = SECRET) -> dict[str, str]:
    payload = f"{int(timestamp)}.{body.decode()}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return {"X-GHL-Signature": digest, "X-GHL-Timestamp": str(int(timestamp))}


def show(label: str, intake) -> None:
    body = adapter_response(intake)
    print(f"{label:<28} -> {intake.http_status} {json.dumps(body)}")


def main() -> None:
    receiver = Receiver([SECRET], replay=ReplayGuard(clock=lambda: NOW))

    body = build_body("evt_100")
    show("valid delivery", receiver.handle(signed(body), body))
    show("same delivery again", receiver.handle(signed(body), body))
    show("forged secret", receiver.handle(signed(body, secret="wrong"), body))
    show("unsigned", receiver.handle({"content-type": "application/json"}, body))
    show("stale timestamp", receiver.handle(signed(body, timestamp=NOW - 600), body))
    show("unknown type", receiver.handle(signed(build_body("evt_101", "SmsOptOut")), build_body("evt_101", "SmsOptOut")))
    show("broken json", receiver.handle(signed(b"not json"), b"not json"))

    print()
    print("Note the third question the response answers: `process` tells the caller")
    print("whether there is work to do. A duplicate is a 200 with process=false —")
    print("acknowledged to the sender, not re-run by the application.")


if __name__ == "__main__":
    main()
