"""Shared plumbing for the hook scripts.

Every hook is a short-lived process the agent spawns. They all: bail out unless
the host is enabled and this is a bridged session, read the project and agent
from the environment, log under the state dir, and talk to the relay through a
short-lived ``RelayTransport`` built from the persisted host identity. The
notification helper here replaces the old ``notify.py`` subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

from aaw_core.config import Settings, load_settings
from aaw_core.host.identity import load_identity
from aaw_core.transport.relay import RelayTransport

# The aaw-core names, and the legacy names an older agent start script may still export.
_PROJECT_VARS = ("AAW_PROJECT", "AGENT_BRIDGE_PROJECT")
_AGENT_VARS = ("AAW_AGENT", "AGENT_BRIDGE_AGENT")

LEVEL_EMOJI = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗"}


def _env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def env_project() -> str:
    return _env_first(_PROJECT_VARS)


def env_agent() -> str:
    return _env_first(_AGENT_VARS, "claude")


def preamble() -> tuple[Settings, str, str]:
    """Exit 0 silently unless the host is enabled and this is a bridged session.

    Hooks are registered globally, so they run for every session on the machine. A
    session the host did not start has no project variable and must never write
    anywhere (an IDE-launched agent in the same repo would otherwise pollute the feed).
    Returns (settings, project_id, agent).
    """
    settings = load_settings()
    if not settings.enabled_flag.exists():
        sys.exit(0)
    project = env_project()
    if not project:
        sys.exit(0)
    return settings, project, env_agent()


def read_payload() -> dict:
    """The hook's JSON payload from stdin, or {} if there is none or it is malformed."""
    try:
        data = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def hook_log(settings: Settings, name: str, msg: str) -> None:
    """Append one timestamped line to <logs dir>/<name>.log. Never raises."""
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # local, tz-aware
        with (settings.logs_dir / f"{name}.log").open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def read_key(settings: Settings) -> str | None:
    """The session key if the host has one. Hooks never create keys."""
    try:
        key = settings.session_key_file.read_text().strip()
    except OSError:
        return None
    return key or None


def open_transport(settings: Settings, project_id: str, *, wait: float = 10.0) -> RelayTransport | None:
    """A short-lived relay transport for one hook run, or None if the host is not linked.

    Callers must ``stop()`` it before exiting (which flushes queued frames)."""
    if not settings.relay_url:
        return None
    ident = load_identity(settings)
    if ident is None:
        return None
    transport = RelayTransport(
        relay_url=settings.relay_url, token=ident.token, computer_id=ident.computer_id,
        project_id=project_id, sessions_dir=settings.sessions_dir,
        computer_name=ident.computer_name, enc_key=read_key(settings),
    ).start()
    if not transport.wait_connected(wait):
        transport.stop(flush_timeout=0)
        return None
    return transport


# ── mobile mode ─────────────────────────────────────────────────────────────


def is_mobile_mode(settings: Settings) -> bool:
    """True while permission prompts should go to the phone ("manual", or a timestamp)."""
    try:
        content = settings.mobile_mode_file.read_text().strip()
        return content == "manual" or bool(float(content))
    except (OSError, ValueError):
        return False


def refresh_mobile_mode(settings: Settings) -> None:
    """Answering from the phone counts as phone activity; extend the timed mode.
    A manually enabled mode has no expiry and is left alone."""
    try:
        if settings.mobile_mode_file.read_text().strip() == "manual":
            return
    except OSError:
        pass
    try:
        settings.mobile_mode_file.parent.mkdir(parents=True, exist_ok=True)
        settings.mobile_mode_file.write_text(str(time.time()))
    except OSError:
        pass


# ── notifications ───────────────────────────────────────────────────────────


def desktop_banner(title: str, message: str) -> None:
    """A banner on the computer itself: osascript on macOS, notify-send on a Linux desktop.
    A headless box has none; the phone is the primary channel anyway. Best effort."""
    try:
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)} sound name \"default\""
            subprocess.run(["osascript", "-e", script], check=False, timeout=5, capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], check=False, timeout=5, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def notify(transport: RelayTransport | None, message: str, level: str = "info", *,
           project: str = "", agent: str = "") -> None:
    """A notification event on the feed (a push on the phone for success/warning/error)
    plus a desktop banner. Replaces the old notify.py subprocess."""
    if transport is not None:
        transport.write_notification(message, level)
    header = " / ".join(p for p in (project, agent) if p)
    title = f"[{header}] Agents At Work" if header else "Agents At Work"
    desktop_banner(title, f"{LEVEL_EMOJI.get(level, LEVEL_EMOJI['info'])} {message}")


# ── tmux ────────────────────────────────────────────────────────────────────

TMUX_PREFIX = "cb-"


def tmux_session(project_id: str) -> str:
    """The tmux session that hosts a bridged project's agent."""
    return f"{TMUX_PREFIX}{project_id}" if project_id else "claude"
