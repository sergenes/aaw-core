"""Run the relay: ``python -m aaw_core.relay --host 0.0.0.0 --port 8765 --db relay.sqlite``.

Configuration is flags or environment (``AAW_RELAY_HOST``, ``AAW_RELAY_PORT``,
``AAW_RELAY_DB``); there are no hosted defaults. Put it behind TLS (a reverse proxy
that terminates ``wss://``) for anything beyond a local test.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from aaw_core.relay.server import create_app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aaw-relay", description="Agents At Work Core relay server")
    p.add_argument("--host", default=os.environ.get("AAW_RELAY_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AAW_RELAY_PORT", "8765")))
    p.add_argument("--db", default=os.environ.get("AAW_RELAY_DB", "relay.sqlite"))
    a = p.parse_args(argv)
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
