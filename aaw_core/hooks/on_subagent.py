"""SubagentStart/SubagentStop hook: the phone shows work still running in the background.

Claude Code fires SubagentStart when a turn spawns a subagent and SubagentStop when one
finishes, which may be after the turn itself ended ("Waiting for 1 background agent to
finish"). The ids of a real subagent match between the two events; Claude also fires
stop-only events for its internal helper agents, so an unknown id's stop is a no-op.
The current set lives in a file the daemon clears on session start, /restart and /stop,
and its ids are mirrored to the project document as ``background_agents``.
Never blocks: always exits 0.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

from aaw_core.config import Settings
from aaw_core.hooks.common import hook_log, open_transport, preamble, read_payload


def subagents_file(settings: Settings, project: str) -> Path:
    return settings.run_dir / f"subagents.{project}.json"


def update_subagents(settings: Settings, project: str, *, add: str | None = None,
                     discard: str | None = None, clear: bool = False) -> list[str]:
    """The set of running subagent ids, updated under a file lock (each hook is its own
    process). Returns the new set, sorted. A discard of an unknown id changes nothing."""
    path = subagents_file(settings, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                ids = set(json.loads(f.read() or "[]"))
            except ValueError:
                ids = set()
            if clear:
                ids = set()
            if add:
                ids.add(add)
            if discard:
                ids.discard(discard)
            out = sorted(ids)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(out))
            return out
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def main() -> int:
    settings, project, _agent = preamble()
    payload = read_payload()
    event = payload.get("hook_event_name", "")
    agent_id = str(payload.get("agent_id") or "")
    if not agent_id:
        return 0
    if event == "SubagentStart":
        ids = update_subagents(settings, project, add=agent_id)
    elif event == "SubagentStop":
        before = update_subagents(settings, project)
        if agent_id not in before:
            return 0  # an internal helper's stop; it was never shown
        ids = update_subagents(settings, project, discard=agent_id)
    else:
        return 0
    hook_log(settings, "on_subagent", f"{event} {agent_id} -> {len(ids)} running")
    transport = open_transport(settings, project)
    try:
        if transport is not None:
            transport.update_project(background_agents=ids)
    finally:
        if transport is not None:
            transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
