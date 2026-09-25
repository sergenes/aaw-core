"""tmux plumbing: every tmux call the host makes goes through here.

Session targets are exact-matched ("=name:"): tmux resolves "-t cb-ideas" by prefix
when no session has that exact name, so with "cb-ideas" stopped and "cb-ideas-claude"
alive, every command aimed at the primary silently hit the parallel session. The
colon makes the target valid for pane and window commands too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from aaw_core.host.detect import dialog_is_open, has_claude_history, pane_input_is_empty, strip_ansi

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
    if agent == "scoot":
        return "scoot --approval auto-read --scope anywhere --continue"
    return "claude --continue" if has_claude_history(project_dir) else "claude"


def restart_agent(session: str, project_dir, agent: str, project_id: str) -> bool:
    """Kill the session and recreate it (reliable whatever state the agent is in). The new
    session carries the same identity: hooks read the project variable to find their feed."""
    print(f"[daemon] /restart: killing session {session}", flush=True)
    tmux_run(["kill-session", "-t", session], capture_output=True)
    time.sleep(0.5)
    if project_dir is None:
        print("[daemon] /restart: no project dir; session killed, not restarted", flush=True)
        return False
    cmd = agent_command(agent, project_dir)
    full_cmd = f"{cmd}; echo '{SESSION_ENDED_SENTINEL}'; read -p 'Press Enter'"
    result = tmux_run([
        "new-session", "-d", "-s", session, "-c", str(project_dir),
        "-e", f"AAW_PROJECT={project_id}", "-e", f"AAW_AGENT={agent}",
        "-e", f"AGENT_BRIDGE_PROJECT={project_id}", "-e", f"AGENT_BRIDGE_AGENT={agent}",  # legacy names
        "-e", f"PATH={os.environ.get('PATH', '')}",
        full_cmd,
    ], capture_output=True, timeout=10)
    if result.returncode != 0:
        print(f"[daemon] /restart: failed to create session: {result.stderr.decode()[:200]}", file=sys.stderr, flush=True)
        return False
    print(f"[daemon] /restart: new session started ({cmd})", flush=True)
    if sys.platform == "darwin":  # best effort: show it in Terminal.app
        try:
            subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "tmux attach -t ={session}"'],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        print(f"[daemon] /restart: attach with: tmux attach -t ={session}", flush=True)
    return True
