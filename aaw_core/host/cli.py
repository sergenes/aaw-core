"""The ``aaw`` command line entry point.

Scaffold: the real CLI (login/link, start, status, feed, schedule, stop, quit,
uninstall) is ported from the host in Phase 2. This stub only makes the console
script resolve and report the version, so ``pipx install`` works from day one.
"""

from __future__ import annotations

import sys

from aaw_core import __version__
from aaw_core.config import load_settings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--version", "-V", "version"):
        print(f"aaw-core {__version__}")
        return 0
    settings = load_settings()
    print(f"aaw-core {__version__} (pre-alpha scaffold)")
    print(f"  state dir : {settings.state_dir}")
    print(f"  relay url : {settings.relay_url or '(not set; export AAW_RELAY_URL)'}")
    print("  The host commands are not ported yet. See CONTRIBUTING.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
