"""Run the mock server: ``python -m ghl_mock --port 8080``."""

from __future__ import annotations

import argparse
import sys

from .app import MockState, create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline HighLevel API mock server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", default=None, help="expected bearer token")
    parser.add_argument(
        "--no-auth", action="store_true", help="accept requests without a token"
    )
    args = parser.parse_args(argv)

    state = MockState(require_auth=not args.no_auth)
    if args.token:
        state.token = args.token

    server = create_server(args.host, args.port, state=state)
    host, port = server.server_address[0], server.server_address[1]
    print(f"ghl-mock listening on http://{host}:{port}", flush=True)
    print(f"  token: {state.token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
