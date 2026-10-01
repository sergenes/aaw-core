"""tmux plumbing: every tmux call the host makes goes through here.

Session targets are exact-matched ("=name:"): tmux resolves "-t aaw-ideas" by prefix
when no session has that exact name, so with "aaw-ideas" stopped and "aaw-ideas-claude"
alive, every command aimed at the primary silently hit the parallel session. The
colon makes the target valid for pane and window commands too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aaw_core.host.detect import (
    dialog_is_open,
    has_claude_history,
    has_scoot_history,
    pane_input_is_empty,
    strip_ansi,
)

_TMUX_BIN = shutil.which("tmux") or "tmux"
SEND_ATTEMPTS = 5
SESSION_ENDED_SENTINEL = "--- session ended ---"


def tmux_run(args: list, **kwargs) -> subprocess.CompletedProcess:
    """Run one tmux command with exact-match session targets. close_fds=False plus an
    absolute path lets CPython use posix_spawn instead of fork+exec."""
    kwargs.setdefault("close_fds", False)
    args = list(args)
    for i, a in enumerate(args[:-1]):
        if a == "-t" and not str(args[i + 1]).startswith("="):
            args[i + 1] = "=" + str(args[i + 1]) + ":"
    return subprocess.run([_TMUX_BIN, *args], check=kwargs.pop("check", False), **kwargs)


def has_session(session: str) -> bool:
    return tmux_run(["has-session", "-t", session], capture_output=True).returncode == 0


def session_confirmed_gone(session: str) -> bool:
    """True only when tmux positively confirms the session is gone: the server answers
    without it in the list, or no server is running at all. has-session fails
    transiently, so any ambiguous error counts as "still there"."""
    try:
        r = tmux_run(["list-sessions", "-F", "#{session_name}"],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode == 0:
        return session not in r.stdout.splitlines()
    err = r.stderr or ""
    return "no server running" in err or "error connecting to" in err


def send_keys(session: str, *keys: str) -> bool:
    """Send key names (e.g. "1", "y", "Down", "Enter") without literal interpretation."""
    r = tmux_run(["send-keys", "-t", session, *keys], capture_output=True, timeout=5)
    return r.returncode == 0


def pane(session: str, scrollback: int = 60) -> str:
    """The pane with some scrollback, ANSI-stripped."""
    try:
        r = tmux_run(["capture-pane", "-t", session, "-p", "-S", f"-{scrollback}"],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return strip_ansi(r.stdout) if r.returncode == 0 else ""


def visible_pane(session: str) -> str:
    """Only the visible pane (no scrollback), ANSI-stripped: what prompt detection reads."""
    try:
        r = tmux_run(["capture-pane", "-t", session, "-p"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return strip_ansi(r.stdout) if r.returncode == 0 else ""


def _paste(text: str, session: str) -> tuple[bool, str]:
    """Deliver text as one bracketed paste via a tmux buffer. Returns (ok, stderr)."""
    buf = f"aaw-{os.getpid()}"
    try:
        r = tmux_run(["load-buffer", "-b", buf, "-"], input=text.encode("utf-8"), capture_output=True, timeout=5)
        if r.returncode != 0:
            return False, (r.stderr or b"").decode(errors="replace").strip()
        r = tmux_run(["paste-buffer", "-p", "-d", "-b", buf, "-t", session], capture_output=True, timeout=5)
        return r.returncode == 0, (r.stderr or b"").decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"


def send_text(text: str, session: str, agent: str = "claude") -> bool:
    """Type a message into the agent and submit it. True on success.

    Text and Enter are separate commands with a pause between: a TUI treats a fast
    burst as a paste and swallows an Enter inside it. Multi-line or long text goes in
    as a bracketed paste so its newlines do not act as Enter. For Claude the pane is
    checked afterwards and Enter is re-sent while the input line still holds text.
    Every tmux call is retried; a message typed but never submitted is reported as
    undelivered rather than retyped, so it is never duplicated."""

    def run(args: list) -> tuple[bool, str]:
        try:
            r = tmux_run(args, capture_output=True, timeout=5)
            return r.returncode == 0, (r.stderr or b"").decode(errors="replace").strip()
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"{type(e).__name__}: {e}"

    if dialog_is_open(visible_pane(session), agent):
        print("[daemon] not typing: a dialog is open in the pane", flush=True)
        return False
    use_paste = ("\n" in text) or len(text) > 200
    typed = False
    for attempt in range(1, SEND_ATTEMPTS + 1):
        ok, err = _paste(text, session) if use_paste else run(["send-keys", "-t", session, "-l", "--", text])
        if ok:
            typed = True
            if attempt > 1:
                print(f"[daemon] tmux send succeeded on attempt {attempt}", flush=True)
            break
        print(f"[daemon] tmux send failed ({attempt}/{SEND_ATTEMPTS}): {err[:160]}", file=sys.stderr, flush=True)
        time.sleep(0.5)
    if not typed:
        return False
    time.sleep(min(2.0, 0.2 + len(text) / 1000.0))  # let the TUI absorb the text before Enter
    for attempt in range(1, SEND_ATTEMPTS + 1):
        ok, err = run(["send-keys", "-t", session, "Enter"])
        if not ok:
            print(f"[daemon] tmux Enter failed ({attempt}/{SEND_ATTEMPTS}): {err[:160]}", file=sys.stderr, flush=True)
            time.sleep(0.5)
            continue
        if agent != "claude":
            return True
        for _ in range(6):  # Claude: confirm the input line emptied
            time.sleep(0.25)
            state = pane_input_is_empty(visible_pane(session))
            if state is None or state:
                if attempt > 1:
                    print(f"[daemon] message submitted after Enter attempt {attempt}", flush=True)
                return True
        print(f"[daemon] input still holds text after Enter ({attempt}/{SEND_ATTEMPTS}); sending Enter again", flush=True)
    return False


def resumes_conversation(agent: str, project_dir) -> bool:
    """True when `agent_command` restarts this agent inside its previous conversation
    (Claude and Scoot with `--continue`, when the folder has history). The other agents
    always start a fresh conversation."""
    if project_dir is None:
        return False
    if agent == "scoot":
        return has_scoot_history(Path(project_dir))
    if agent in ("gemini", "grok", "cursor", "codex"):
        return False
    return has_claude_history(project_dir)


def agent_command(agent: str, project_dir) -> str:
    """The command line that starts an agent in a fresh tmux session."""
    if agent == "gemini":
        return "command gemini --yolo"  # bypass a shell function that would start a second host
    if agent == "grok":
        return "grok --yolo"
    if agent == "cursor":
        return "cursor-agent --trust"  # never bare "agent": Grok installs a binary by that name
    if agent == "codex":
        return "codex"
    resume = " --continue" if resumes_conversation(agent, project_dir) else ""
    if agent == "scoot":
        return f"scoot --approval auto-read --scope anywhere{resume}"
    return f"claude{resume}"


def create_session(session: str, project_dir, agent: str, project_id: str,
                   extra_env: dict[str, str] | None = None) -> tuple[bool, str]:
    """A detached tmux session running the agent, tagged with the session's identity so
    the hooks find their feed. Returns (ok, error text).

    -e PATH: a tmux session inherits the tmux *server's* environment, and that server may
    have been spawned by the supervisor (a systemd user service, PATH without ~/.local/bin),
    so "claude" would be "command not found" in the pane even though the launching shell
    finds it. The agent's own exit leaves a sentinel line and a paused shell behind, so a
    crash stays readable and the daemon can tell "ended" from "still starting"."""
    cmd = agent_command(agent, project_dir)
    full_cmd = f"{cmd}; echo '{SESSION_ENDED_SENTINEL}'; read -p 'Press Enter'"
    env = {"AAW_PROJECT": project_id, "AAW_AGENT": agent, "PATH": os.environ.get("PATH", "")}
    env.update(extra_env or {})
    args = ["new-session", "-d", "-s", session, "-c", str(project_dir)]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    try:
        result = tmux_run([*args, full_cmd], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    if result.returncode != 0:
        return False, (result.stderr or b"").decode(errors="replace").strip()[:200]
    return True, ""


def restart_agent(session: str, project_dir, agent: str, project_id: str) -> bool:
    """Kill the session and recreate it (reliable whatever state the agent is in). The new
    session carries the same identity: hooks read the project variable to find their feed."""
    print(f"[daemon] /restart: killing session {session}", flush=True)
    tmux_run(["kill-session", "-t", session], capture_output=True)
    time.sleep(0.5)
    if project_dir is None:
        print("[daemon] /restart: no project dir; session killed, not restarted", flush=True)
        return False
    ok, err = create_session(session, project_dir, agent, project_id)
    if not ok:
        print(f"[daemon] /restart: failed to create session: {err}", file=sys.stderr, flush=True)
        return False
    print(f"[daemon] /restart: new session started ({agent_command(agent, project_dir)})", flush=True)
    if sys.platform == "darwin":  # best effort: show it in Terminal.app
        try:
            subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "tmux attach -t ={session}"'],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        print(f"[daemon] /restart: attach with: tmux attach -t ={session}", flush=True)
    return True
