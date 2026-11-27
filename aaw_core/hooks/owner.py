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

Codex is different (2026-11-26, CLI 0.162 with the shared app-server daemon 0.172). It fires
every hook, and runs every shell command, from one daemon shared by all its sessions, and
that daemon keeps the environment of whichever session started it. ``AAW_PROJECT`` in a
Codex hook therefore names the first Codex session of the day, not the one the hook is
about: a second session's prompts and approval cards went to the first one's feed. A Codex
hook finds its session through its own input instead: the folder (``cwd``) picks the bridged
Codex session, and the conversation's own record says whether it is the interactive agent
or one started with ``codex exec``, which is never the session's.
"""

from __future__ import annotations

import json
import os
import subprocess

from aaw_core.config import Settings

# Agents whose process layout was checked end to end; the others keep the old behavior.
# Claude Code fires its hooks from the agent process itself. So does Gemini CLI (0.70,
# checked 2026-11-26): the session's hooks come from its worker process (a child of the
# `gemini` launcher), and a `gemini -p` it runs through its shell tool fires its own from
# further down the same tree. Codex fires them from a daemon outside the tree and is
# handled by codex_session() below.
FILTERED_AGENTS = ("claude", "gemini")
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
        if firing <= 1:
            # Unknown, or init: the process that started this hook has already exited (seen
            # with Gemini 0.70 on 2026-11-26, a hook at session start). Init is above every
            # process, so as the owner it would make every later hook look nested.
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


# ── Codex: the session comes from the hook's input, not from the environment ─────────────

CODEX_ROLLOUT_PREFIX = "rollout-"  # ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl
_META_MAX_BYTES = 4 * 1024 * 1024  # the first record carries the base instructions


def _transcript_name(payload: dict) -> str:
    return os.path.basename(str(payload.get("transcript_path") or ""))


def is_codex_hook(payload: dict, env_agent: str) -> bool:
    """Codex fired this hook: its conversation file is a Codex rollout. A hook with no
    conversation file counts as Codex's when the environment says so."""
    name = _transcript_name(payload)
    return name.startswith(CODEX_ROLLOUT_PREFIX) if name else env_agent == "codex"


def foreign_agent(payload: dict, env_agent: str) -> str:
    """"" unless another agent fired this hook from inside a Codex environment (an agent a
    Codex session started inherits it); then a short description for the log."""
    name = _transcript_name(payload)
    if env_agent == "codex" and name and not name.startswith(CODEX_ROLLOUT_PREFIX):
        return f"another agent's conversation ({name[:24]}) under a Codex environment"
    return ""


def _same_folder(a: str, b: str) -> bool:
    try:
        return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _codex_started_by_exec(payload: dict) -> str:
    """"" unless the conversation was started with `codex exec` (its first record says so);
    then a short description. An unreadable record counts as the interactive agent."""
    path = str(payload.get("transcript_path") or "")
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            meta = json.loads(f.readline(_META_MAX_BYTES))
        body = meta.get("payload") if isinstance(meta, dict) else None
        if not isinstance(body, dict):
            return ""
        originator, source = body.get("originator"), body.get("source")
    except (OSError, ValueError):
        return ""
    if originator == "codex_exec" or source == "exec":
        return f"codex exec conversation {session_id_of(payload)[:8] or '?'}"
    return ""


def codex_session(settings: Settings, payload: dict, env_project: str) -> tuple[str, str]:
    """Which session a Codex hook belongs to: (project, why it is ignored). Both empty when
    no bridged Codex session runs in the hook's folder. Never raises.

    The folder decides. The environment's project is used only when the folder finds
    nothing and that project is a Codex session whose recorded folder does not say
    otherwise: the old behavior, kept for a record with no folder in it."""
    try:
        nested = _codex_started_by_exec(payload)
        cwd = str(payload.get("cwd") or "")
        from aaw_core.host import state  # here, not at load: every hook of every agent imports this module

        docs = state.read_projects(state.mirror_dir(settings.state_dir))
        codex = {p: d for p, d in docs.items() if (d.get("agent") or "") == "codex"}
        here = [p for p, d in codex.items() if _same_folder(str(d.get("project_path") or ""), cwd)]
        running = [p for p in here if (codex[p].get("status") or "stopped") != "stopped"]
        project = ""
        if len(running) == 1:
            project = running[0]
        elif env_project in here:
            project = env_project
        elif not here and env_project:
            doc = docs.get(env_project)
            if doc is None or ((doc.get("agent") or "codex") == "codex" and not (doc.get("project_path") and cwd)):
                project = env_project
        if project and nested:
            return project, nested
        return project, ""
    except Exception:  # noqa: BLE001 - a hook must never fail on bookkeeping
        return env_project, ""
