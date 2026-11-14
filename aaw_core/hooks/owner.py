"""Whose hook is this: the session's own agent, or an agent it started?

Hooks are registered globally and find their session through ``AAW_PROJECT``, which every
process the agent starts inherits. An agent that runs another agent through its shell tool
(``claude -p "..."`` from a script, seen 2026-11-14) therefore fired these hooks as the
session itself: the child's prompt appeared in the feed as a user message nobody sent, its
Stop pushed "Response ready" and wrote idle while the real turn was still running, and its
permission prompts went to the phone.

The session's own agent is remembered by process: the first agent to fire a hook for the
project, with the conversation ids it has used. A later hook is a nested agent's when the
process that fired it descends from that owner and its conversation is not one of the
owner's. Everything uncertain counts as the owner's: a wrongly dropped hook silences a
real session, a wrongly kept one is the old behavior.
"""

from __future__ import annotations

import json
import os
import subprocess

from aaw_core.config import Settings

# Agents whose process layout was checked end to end; the others keep the old behavior.
# Claude Code fires its hooks from the agent process itself. Codex (0.164) fires them from
# an app-server process beside the TUI, which this check would have to understand first.
FILTERED_AGENTS = ("claude",)
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_MAX_SESSIONS = 20  # /clear and /resume each add one; old ones stop mattering

Table = dict[int, tuple[int, str, str]]  # pid -> (parent pid, start time, command name)


def process_table() -> Table:
    """Every process: its parent, start time and command name. {} when ps cannot be read."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,comm="], capture_output=True, text=True, check=False,
                             timeout=5, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: Table = {}
    for line in out.splitlines():
        parts = line.split(None, 7)  # pid ppid + lstart's five fields + the command
        if len(parts) < 8:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), " ".join(parts[2:7]), parts[7])
        except ValueError:
            continue
    return table


def _is_shell(command: str) -> bool:
    return os.path.basename(command).lstrip("-") in SHELLS


def firing_agent(table: Table, pid: int) -> int:
    """The process that fired the hook running as `pid`: its nearest ancestor that is not
    a shell (agents start hooks directly or through ``sh -c``). 0 when unknown."""
    seen = set()
    parent = table.get(pid, (0, "", ""))[0]
    while parent in table and parent not in seen and _is_shell(table[parent][2]):
        seen.add(parent)
        parent = table[parent][0]
    return parent if parent in table else 0


def is_ancestor(table: Table, ancestor: int, pid: int) -> bool:
    """`ancestor` is strictly above `pid` in the process tree."""
    seen = set()
    current = table.get(pid, (0, "", ""))[0]
    while current in table and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = table[current][0]
    return False


def owner_file(settings: Settings, project: str):
    return settings.run_dir / f"owner.{project}.json"


def _read(settings: Settings, project: str) -> dict:
    try:
        data = json.loads(owner_file(settings, project).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(settings: Settings, project: str, record: dict) -> None:
    path = owner_file(settings, project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record))
        os.replace(tmp, path)
    except OSError:
        pass


def session_id_of(payload: dict) -> str:
    return str(payload.get("session_id") or payload.get("conversation_id") or "")


def nested_agent(settings: Settings, project: str, agent: str, payload: dict, *,
                 table: Table | None = None, pid: int | None = None) -> str:
    """"" when this hook belongs to the session's own agent; otherwise a short description
    of the nested agent that fired it, for the log. Never raises."""
    if agent not in FILTERED_AGENTS:
        return ""
    try:
        table = process_table() if table is None else table
        firing = firing_agent(table, os.getpid() if pid is None else pid)
        if not firing:
            return ""
        session = session_id_of(payload)
        record = _read(settings, project)
        owner = record.get("pid")
        owner_alive = isinstance(owner, int) and owner in table and table[owner][1] == record.get("start")
        if not owner_alive or is_ancestor(table, firing, owner):
            # No owner yet, the owner exited (a restart), or the remembered one turns out
            # to be a child of this agent: this agent is the session's.
            _write(settings, project, {"pid": firing, "start": table[firing][1],
                                       "sessions": [session] if session else []})
            return ""
        sessions = [s for s in record.get("sessions", []) if isinstance(s, str)]
        if firing == owner:
            if session and session not in sessions:
                _write(settings, project, {**record, "sessions": [*sessions, session][-_MAX_SESSIONS:]})
            return ""
        if is_ancestor(table, owner, firing) and session and session not in sessions:
            return f"pid {firing} ({os.path.basename(table[firing][2])}), session {session[:8]}, under owner pid {owner}"
        return ""
    except Exception:  # noqa: BLE001 - a hook must never fail on bookkeeping
        return ""
