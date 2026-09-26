"""Back up the relay store while the relay runs.

``python -m aaw_core.relay.backup --db /var/lib/aaw-relay/relay.sqlite --dir /var/lib/aaw-relay/backups --keep 14``

Uses SQLite's online backup API, so the copy is consistent even while the relay writes
(the store is in WAL mode). Writes ``relay-YYYYMMDD-HHMMSS.sqlite`` into ``--dir`` and
keeps the newest ``--keep`` files. The store holds encrypted events, commands and device
tokens; losing it costs replay and re-pairing, never a key, so a daily copy is enough.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path


def backup(db: str, directory: str, keep: int = 14, now: float | None = None) -> Path:
    """Copy ``db`` into ``directory`` and prune to the newest ``keep`` copies; returns the new file."""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    target = out_dir / f"relay-{stamp}.sqlite"
    tmp = target.with_suffix(".sqlite.part")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    copies = sorted(out_dir.glob("relay-*.sqlite"))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aaw-relay-backup", description="Back up the relay store")
    p.add_argument("--db", default=os.environ.get("AAW_RELAY_DB", "relay.sqlite"))
    p.add_argument("--dir", default=os.environ.get("AAW_RELAY_BACKUP_DIR", "backups"))
    p.add_argument("--keep", type=int, default=14, help="how many copies to keep (default 14)")
    a = p.parse_args(argv)
    target = backup(a.db, a.dir, a.keep)
    print(f"[relay] backup {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
