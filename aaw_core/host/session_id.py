"""The one place a session id is minted.

A session is identified by folder and agent. The id is the feed id on the relay, the
tmux name minus its prefix, and the stem of the daemon's pid and log files.

Rules, in order:
  1. same path, same agent, live            -> attach to it
  2. same path, same agent, stopped doc     -> reuse its id (a restart keeps its history)
  3. bare basename owned by another path    -> "<basename>-<parent>" (numeric fallback)
  4. nothing else live on the path          -> the base, as always
  5. another agent live on the path         -> "<base>-<agent>"
  6. never an id owned by a different path
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Known:
    """One session the host knows about: live in tmux, or a stopped project document."""

    id: str
    path: str
    agent: str | None
    is_live: bool


@dataclass(frozen=True)
class Resolution:
    action: str  # "attach" | "launch"
    id: str
    alongside: tuple  # live sessions of other agents on the same path (launch only)


def _owned_by_another_path(sid: str, path: str, known: list[Known]) -> bool:
    return any(k.id == sid and k.path and k.path != path for k in known)


def _first_free(candidate: str, path: str, known: list[Known]) -> str:
    if not _owned_by_another_path(candidate, path, known):
        return candidate
    for n in range(2, 100):
        nxt = f"{candidate}-{n}"
        if not _owned_by_another_path(nxt, path, known):
            return nxt
    return f"{candidate}-{int(time.time())}"


def sanitize(s: str) -> str:
    """tmux rewrites "." and ":" in session names; keep ids to safe characters."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", s).strip("-")


def resolve(path: str, agent: str, known: list[Known]) -> Resolution:
    on_path = [k for k in known if k.path and k.path == path]
    live = next((k for k in on_path if k.is_live and k.agent == agent), None)
    if live:
        return Resolution("attach", live.id, ())
    live_others = tuple(k for k in on_path if k.is_live and k.agent != agent)
    stopped = next((k for k in on_path if not k.is_live and k.agent == agent), None)
    if stopped:
        return Resolution("launch", stopped.id, live_others)
    basename = sanitize(Path(path).name) or "session"
    base = basename
    if _owned_by_another_path(base, path, known):
        parent = sanitize(Path(path).parent.name)
        base = _first_free(f"{basename}-{parent}" if parent else basename, path, known)
    if not live_others:
        return Resolution("launch", base, ())
    return Resolution("launch", _first_free(f"{base}-{agent}", path, known), live_others)
