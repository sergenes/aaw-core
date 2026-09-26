"""Session lifecycle on the computer: the tmux session, its daemon, and the keep-awake helper.

Everything the shell start/stop scripts used to do lives here, in one place, so the
rules (which daemon to kill, when the keep-awake helper is released) exist once and
the ``aaw`` command, the supervisor, and the shell integration all share them.

Process discovery goes through ``ps -A -o pid=,args=`` (POSIX, the same on macOS and
Linux) rather than pgrep patterns: a daemon is recognized by the module it runs and the
exact ``--project-id <id> --agent`` pair on its command line, so two sessions on one
folder (Claude and Codex in parallel) never kill each other's daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from aaw_core.config import Settings
from aaw_core.hooks.common import TMUX_PREFIX, tmux_session
from aaw_core.host import tmux
from aaw_core.host.session_id import Known

DAEMON_MODULE = "aaw_core.daemon"
LOG_ROTATE_BYTES = 20 * 1024 * 1024
STOP_GRACE_S = 8.0  # the daemon notices tmux is gone within ~3 s and writes "stopped" itself

AGENTS = ("claude", "codex", "gemini", "grok", "cursor", "scoot")
AGENT_BINARIES = {"claude": "claude", "codex": "codex", "gemini": "gemini",
                  "grok": "grok", "cursor": "cursor-agent", "scoot": "scoot"}


class SessionError(Exception):
    """A start or stop that cannot proceed; the message is for the user."""


@dataclass
class Session:
    id: str  # tmux name, e.g. aaw-articles
    project: str  # id without the prefix, e.g. articles
    path: str
    attached: bool


# ── processes ────────────────────────────────────────────────────────────────


def _processes() -> list[tuple[int, str]]:
    """(pid, command line) of every process visible to this user."""
    try:
        # -ww: procps truncates args to 80 columns when stdout is not a tty (a service, CI)
        r = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,args="], capture_output=True, text=True,
                           timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1]))
    return out


def _alive(pid: int) -> bool:
    """False once the process has exited. A child of this process that exited is a zombie
    until reaped, and kill -0 still answers for it (macOS has no /proc to tell), so reap
    our own children here: the supervisor spawns daemons and must see them go."""
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Linux: a child nobody reaped answers kill -0 but is dead (seen in a container
    whose PID 1 was `sleep`). No /proc elsewhere, so False there."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def _terminate(pid: int, grace: float) -> None:
    """SIGTERM, wait up to `grace`, then SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


# ── tmux sessions ─────────────────────────────────────────────────────────────


def list_sessions() -> list[Session]:
    r = tmux.tmux_run(["list-panes", "-a", "-F", "#{session_name}|#{session_attached}|#{pane_current_path}"],
                      capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return []
    seen, out = set(), []
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].startswith(TMUX_PREFIX) or parts[0] in seen:
            continue
        seen.add(parts[0])
        out.append(Session(parts[0], parts[0][len(TMUX_PREFIX):], parts[2], parts[1] == "1"))
    return sorted(out, key=lambda s: s.project)


def session_alive(project: str) -> bool:
    return tmux.has_session(tmux_session(project))


def session_agent(project: str) -> str | None:
    """The agent a live session was created for (its AAW_AGENT). A session with no
    recorded agent counts as claude."""
    r = tmux.tmux_run(["show-environment", "-t", tmux_session(project), "AAW_AGENT"], capture_output=True, text=True)
    if r.returncode == 0 and "=" in r.stdout:
        value = r.stdout.strip().split("=", 1)[1]
        if value:
            return value
    return "claude" if session_alive(project) else None


def session_path(project: str) -> str | None:
    r = tmux.tmux_run(["display-message", "-p", "-t", tmux_session(project), "#{pane_current_path}"],
                      capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def attach_command(project: str) -> str:
    return f"tmux attach -t ={tmux_session(project)}"


# ── daemons ───────────────────────────────────────────────────────────────────


def _daemon_processes(project: str) -> list[int]:
    """Pids of every daemon carrying exactly this session id."""
    needle = f"--project-id {project} --agent"
    return [pid for pid, args in _processes() if DAEMON_MODULE in args and needle in args]


def daemon_pid(settings: Settings, project: str) -> int | None:
    pid = _read_pid(settings.daemon_pid_file(project))
    if pid and _alive(pid):
        return pid
    found = _daemon_processes(project)
    return found[0] if found else None


def running_daemon_agent(settings: Settings, project: str) -> str | None:
    pid = daemon_pid(settings, project)
    if not pid:
        return None
    for p, args in _processes():
        if p == pid:
            words = args.split()
            if "--agent" in words and words.index("--agent") + 1 < len(words):
                return words[words.index("--agent") + 1]
    return None


def _rotate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def start_daemon(settings: Settings, project_dir: Path, project: str, agent: str) -> list[str]:
    """Kill any daemon for this id (pid file, then any process with the id on its command
    line) and start a fresh one. Returns the log lines."""
    out = [f"starting daemon for {project}"]
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    pid_file = settings.daemon_pid_file(project)
    old = _read_pid(pid_file)
    if old and _alive(old):
        out.append(f"  stopping the previous daemon (pid {old})")
        _terminate(old, 5.0)
    pid_file.unlink(missing_ok=True)
    for pid in _daemon_processes(project):
        _terminate(pid, 2.0)
        out.append(f"  stopped an orphaned daemon (pid {pid})")
    log_file = settings.daemon_log_file(project)
    _rotate_log(log_file)
    cmd = [sys.executable, "-m", DAEMON_MODULE, "--project-dir", str(project_dir),
           "--project-id", project, "--agent", agent]
    with log_file.open("ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
    pid_file.write_text(str(proc.pid))
    out.append(f"  daemon started (pid {proc.pid}); log: {log_file}")
    return out


def stop_daemon(settings: Settings, project: str, grace: float = STOP_GRACE_S) -> list[str]:
    """Stop this session's daemon only. Called after its tmux session is gone, so give the
    daemon a moment to notice and write "stopped" itself before terminating it."""
    pid_file = settings.daemon_pid_file(project)
    pids = set(_daemon_processes(project))
    known = _read_pid(pid_file)
    if known and _alive(known):
        pids.add(known)
    pid_file.unlink(missing_ok=True)
    if not pids:
        return [f"  no daemon running for {project}"]
    deadline = time.time() + grace
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.25)
    for pid in pids:
        if _alive(pid):
            _terminate(pid, 3.0)
    return [f"  daemon stopped for {project}"]


# ── keep-awake ────────────────────────────────────────────────────────────────


def _keep_awake_cmd() -> list[str] | None:
    if sys.platform == "darwin":
        # Spelled out, not "-dims": the commercial host adopts any "caffeinate -dims" as
        # its own and kills it when its last session ends, and this one would do the same
        # to it. Distinct command lines keep the two helpers apart.
        return ["caffeinate", "-d", "-i", "-m", "-s"]
    if shutil.which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=idle:sleep", "--who=aaw-core", "--why=agent-session",
                "sleep", "infinity"]
    return None  # a Linux box without systemd; servers do not sleep


def start_keep_awake(settings: Settings) -> str:
    """Keep the computer awake while a session runs. One helper is shared by every session;
    an instance with exactly our command line is adopted, never a user's own."""
    if not settings.keep_awake:
        return "  keep-awake disabled in settings"
    cmd = _keep_awake_cmd()
    if cmd is None:
        return "  no keep-awake tool on this system; skipping"
    pid_file = settings.keep_awake_pid_file
    pid = _read_pid(pid_file)
    if pid and _alive(pid):
        return f"  keep-awake already running (pid {pid})"
    wanted = " ".join(cmd)
    for p, args in _processes():
        if args == wanted:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(p))
            return f"  keep-awake already running (pid {p}, adopted)"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"  keep-awake failed to start: {e}"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))
    return f"  keep-awake started (pid {proc.pid})"


def stop_keep_awake_if_idle(settings: Settings) -> str:
    """Release the shared helper only once no bridged session is left."""
    if list_sessions():
        return "  keep-awake left running (other sessions still active)"
    pid_file = settings.keep_awake_pid_file
    pid = _read_pid(pid_file)
    pid_file.unlink(missing_ok=True)
    if pid and _alive(pid):
        _terminate(pid, 2.0)
        return f"  keep-awake stopped (pid {pid})"
    return "  keep-awake was not running"


# ── sessions ─────────────────────────────────────────────────────────────────


def start_session(settings: Settings, project_dir: Path, project: str, agent: str, *,
                  model: str | None = None) -> list[str]:
    """Bring up one session: keep-awake, the tmux session (left alone if it already
    exists), then its daemon, in that order so the daemon's first check never sees a
    session that is not there yet. Returns the log lines; raises SessionError."""
    if not project_dir.is_dir():
        raise SessionError(f"project folder not found: {project_dir}\n"
                           "  The folder was moved or deleted, or is not mounted.")
    if agent not in AGENTS:
        raise SessionError(f"agent must be one of {', '.join(AGENTS)}")
    if not shutil.which("tmux"):
        raise SessionError("tmux is not installed (brew install tmux / apt install tmux)")
    if not settings.enabled_flag.exists():
        raise SessionError("the host is off; start the supervisor first (aaw supervisor, or the service)")
    session = tmux_session(project)
    out = [start_keep_awake(settings)]
    if tmux.has_session(session):
        out.append(f"  tmux session '{session}' already exists; leaving it running")
    else:
        extra = {"SCOOT_MODEL": model} if (agent == "scoot" and model) else None
        ok, err = tmux.create_session(session, project_dir, agent, project, extra)
        if not ok:
            raise SessionError(f"could not create the tmux session: {err}")
        out.append(f"  tmux session '{session}' started in {project_dir}")
    out += start_daemon(settings, project_dir, project, agent)
    out += ["", f"session ready: {project}", f"  tmux session : {session}",
            f"  attach with  : {attach_command(project)}", f"  stop with    : aaw stop {project}"]
    return out


def stop_session(settings: Settings, project: str) -> list[str]:
    """Stop one session: tmux first (so the daemon sees it go and writes "stopped"),
    then the daemon, then the shared keep-awake if nothing is left."""
    session = tmux_session(project)
    out = [f"stopping session {project}"]
    if tmux.has_session(session):
        tmux.tmux_run(["kill-session", "-t", session], capture_output=True)
        out.append(f"  tmux session '{session}' killed")
    else:
        out.append(f"  tmux session '{session}' was not running")
    out += stop_daemon(settings, project)
    out.append(stop_keep_awake_if_idle(settings))
    return out


def known_sessions(project_docs: dict[str, dict], decrypt) -> list[Known]:
    """What the id resolver needs: live tmux sessions (authoritative) plus the stopped
    project documents from the relay, with their paths decrypted by `decrypt` (a
    ciphertext this key cannot read becomes "", never a foreign folder)."""
    known, live_ids = [], set()
    for s in list_sessions():
        known.append(Known(s.project, s.path, session_agent(s.project), True))
        live_ids.add(s.project)
    for pid, doc in project_docs.items():
        if pid in live_ids or pid.startswith("_"):
            continue
        known.append(Known(pid, decrypt(doc.get("project_path") or ""), doc.get("agent"), False))
    return known


# ── scoot ─────────────────────────────────────────────────────────────────────


def scoot_installed() -> bool:
    return shutil.which("scoot") is not None


def scoot_models() -> list[str] | None:
    """Model ids the installed scoot can serve (provider/model), or None when scoot is absent."""
    if not scoot_installed():
        return None
    try:
        r = subprocess.run(["scoot", "models", "--json"], capture_output=True, text=True, timeout=30, check=False)
        data = json.loads(r.stdout or "{}")
        return sorted(m["id"] for m in data.get("models", []) if m.get("id"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
