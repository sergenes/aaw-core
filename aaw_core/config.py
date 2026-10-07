"""Configuration for the aaw-core host.

Config-first by construction: everything comes from environment variables or
``~/.aaw/config.json``. There are no defaults that point at any hosted project,
which is what makes this repository safe to publish without a secrets audit.

Precedence: environment variable > config file > built-in default.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# A distinct state directory so aaw-core can coexist on one machine with the
# commercial Agents At Work host, which uses ~/.agent-bridge.
DEFAULT_STATE_DIR = Path.home() / ".aaw"

# The relay we host for free users. A public endpoint, not a secret, and never a
# silent default: `aaw link` offers it and writes the choice to config.json.
HOSTED_RELAY_URL = "wss://relay.agentsatwork.app/v1/ws"


def default_computer_name() -> str:
    """The name a person knows this computer by.

    On macOS that is the Computer Name from System Settings ("Sergey's MacBook Pro"),
    not the network hostname ("Sergeys-MacBook-Pro.local"). Elsewhere the hostname,
    without a trailing ".local".
    """
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True,
                                 timeout=2, check=False).stdout.strip()
            if out:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    name = os.uname().nodename
    return name.removesuffix(".local")


def _config_file(state_dir: Path) -> dict:
    path = state_dir / "config.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one host process."""

    state_dir: Path
    relay_url: str | None  # wss://... ; required to bridge to a phone, None = local only
    computer_name: str
    keep_awake: bool
    browse_roots: tuple[str, ...] = ("~",)  # folders the phone may browse and start sessions in
    local_notifications: bool = True  # desktop banners (osascript / notify-send)
    waiting_alert_seconds: int = 0  # 0 = no "waiting for your answer" desktop alert

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def attachments_dir(self) -> Path:
        """Images the phone attaches to a prompt land here (decrypted), and the prompt
        references the path. Swept on a TTL; never a session's own files."""
        return self.state_dir / "attachments"

    @property
    def run_dir(self) -> Path:
        """Pid files of the daemons and the keep-awake helper."""
        return self.state_dir / "run"

    def daemon_pid_file(self, project_id: str) -> Path:
        return self.run_dir / f"daemon.{project_id}.pid"

    def daemon_log_file(self, project_id: str) -> Path:
        return self.logs_dir / f"daemon.{project_id}.log"

    @property
    def keep_awake_pid_file(self) -> Path:
        return self.run_dir / "keep-awake.pid"

    @property
    def supervisor_log_file(self) -> Path:
        return self.logs_dir / "supervisor.log"

    @property
    def session_key_file(self) -> Path:
        return self.state_dir / "session.key"

    @property
    def enabled_flag(self) -> Path:
        return self.state_dir / "enabled"

    @property
    def mobile_mode_file(self) -> Path:
        """Holds "manual" or a timestamp while permission prompts should go to the phone."""
        return self.state_dir / "mobile_mode"

    @property
    def host_file(self) -> Path:
        """The persisted host identity (computer id + relay routing token)."""
        return self.state_dir / "host.json"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"


def save_config(state_dir: Path, **fields) -> Path:
    """Merge fields into <state dir>/config.json (created if missing)."""
    path = state_dir / "config.json"
    current = _config_file(state_dir)
    current.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n")
    return path


def load_settings() -> Settings:
    state_dir = Path(os.environ.get("AAW_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
    cfg = _config_file(state_dir)

    def pick(env: str, key: str, default):
        value = os.environ.get(env)
        if value is not None and value != "":
            return value
        return cfg.get(key, default)

    def flag(env: str, key: str, default: str) -> bool:
        return str(pick(env, key, default)).lower() in ("1", "true", "yes")

    roots = pick("AAW_BROWSE_ROOTS", "browse_roots", "~")
    if isinstance(roots, str):
        roots = [r for r in roots.split(os.pathsep) if r]
    try:
        waiting = int(pick("AAW_WAITING_ALERT_SECONDS", "waiting_alert_seconds", 0))
    except (TypeError, ValueError):
        waiting = 0
    return Settings(
        state_dir=state_dir,
        relay_url=pick("AAW_RELAY_URL", "relay_url", None),
        computer_name=pick("AAW_COMPUTER_NAME", "computer_name", None) or default_computer_name(),
        keep_awake=flag("AAW_KEEP_AWAKE", "keep_awake", "true"),
        browse_roots=tuple(str(r) for r in roots) or ("~",),
        local_notifications=flag("AAW_LOCAL_NOTIFICATIONS", "local_notifications", "true"),
        waiting_alert_seconds=max(0, waiting),
    )
