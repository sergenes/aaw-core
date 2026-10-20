"""SubagentStart/SubagentStop hook: the phone shows work still running in the background.

Claude Code fires SubagentStart when a turn spawns a subagent and SubagentStop when one
finishes, which may be after the turn itself ended ("Waiting for 1 background agent to
finish"). The ids of a real subagent match between the two events; Claude also fires
stop-only events for its internal helper agents, so an unknown id's stop is a no-op.
The current set lives in a file the daemon clears on session start, /restart, /stop and
when the tmux session is confirmed gone; its ids are mirrored to the project document as
``background_agents``. Hook events can outlive the session that fired them (a kill while
a turn is winding down), so a SubagentStart whose tmux session no longer exists is
dropped rather than allowed to resurrect state for a dead session.

Claude fires no SubagentStop when a background agent ends on a usage limit or out of
credits, and fires SubagentStart again (same id) each time the coordinator resumes one,
so the set alone can hold an id forever. Claude does record the end in the agent's
own ``agent-<id>.meta.json`` (``"finished": true``) next to the session transcript, so
the set is reconciled against those files: here on every event, in the Stop hook at the
end of each turn, and on a slow daemon tick for a session that stays idle. The session
transcript path, which every Claude hook payload carries, is kept in a sidecar file so
the daemon can find the records without a hook.
Never blocks: always exits 0.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

from aaw_core.config import Settings
from aaw_core.hooks.common import hook_log, open_transport, preamble, read_payload, tmux_session
from aaw_core.host.tmux import session_confirmed_gone


def subagents_file(settings: Settings, project: str) -> Path:
    return settings.run_dir / f"subagents.{project}.json"


def transcript_file(settings: Settings, project: str) -> Path:
    """Sidecar holding the Claude session transcript path last seen by a hook."""
    return settings.run_dir / f"subagents.{project}.transcript"


def remember_transcript(settings: Settings, project: str, transcript_path: str) -> None:
    if not transcript_path:
        return
    path = transcript_file(settings, project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() == transcript_path:
            return
        path.write_text(transcript_path)
    except OSError:
        pass


def agents_dir(transcript_path: str) -> Path | None:
    """Claude keeps a session's subagent records in <transcript stem>/subagents/."""
    if not transcript_path:
        return None
    t = Path(transcript_path)
    return t.with_suffix("") / "subagents" if t.suffix == ".jsonl" else t / "subagents"


def finished_ids(ids: list[str], transcript_path: str) -> list[str]:
    """The ids among ``ids`` whose Claude record says the agent finished. An id with no
    record, or an unreadable one, is not finished (nothing is dropped on a guess)."""
    d = agents_dir(transcript_path)
    if d is None or not d.is_dir():
        return []
    done = []
    for agent_id in ids:
        try:
            meta = json.loads((d / f"agent-{agent_id}.meta.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("finished") is True:
            done.append(agent_id)
    return done


def reconcile_subagents(settings: Settings, project: str, transcript_path: str | None = None) -> list[str] | None:
    """Drop every id whose agent Claude recorded as finished. Returns the new set when
    something was dropped, else None. Without a transcript path the sidecar is used."""
    if transcript_path is None:
        try:
            transcript_path = transcript_file(settings, project).read_text().strip()
        except OSError:
            return None
    ids = update_subagents(settings, project)
    if not ids:
        return None
    done = finished_ids(ids, transcript_path)
    if not done:
        return None
    for agent_id in done:
        ids = update_subagents(settings, project, discard=agent_id)
    hook_log(settings, "on_subagent", f"reconcile: {', '.join(done)} finished per Claude -> {len(ids)} running")
    return ids


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
    transcript_path = str(payload.get("transcript_path") or "")
    if event == "SubagentStart":
        if session_confirmed_gone(tmux_session(project)):
            hook_log(settings, "on_subagent",
                     f"{event} {agent_id} dropped: the tmux session is gone")
            return 0
        remember_transcript(settings, project, transcript_path)
        ids = update_subagents(settings, project, add=agent_id)
    elif event == "SubagentStop":
        before = update_subagents(settings, project)
        if agent_id not in before:
            return 0  # an internal helper's stop; it was never shown
        ids = update_subagents(settings, project, discard=agent_id)
    else:
        return 0
    hook_log(settings, "on_subagent", f"{event} {agent_id} -> {len(ids)} running")
    # Other agents in the set may have died without a Stop (usage limit); the one that
    # just started or stopped is not finished by its own record yet, so this is safe.
    ids = reconcile_subagents(settings, project, transcript_path or None) or ids
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
