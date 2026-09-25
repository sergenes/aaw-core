"""Configuration for the aaw-core host.

Config-first by construction: everything comes from environment variables or
``~/.aaw/config.json``. There are no defaults that point at any hosted project,
which is what makes this repository safe to publish without a secrets audit.

Precedence: environment variable > config file > built-in default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# A distinct state directory so aaw-core can coexist on one machine with the
# commercial Agents At Work host, which uses ~/.agent-bridge.
DEFAULT_STATE_DIR = Path.home() / ".aaw"


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

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def session_key_file(self) -> Path:
        return self.state_dir / "session.key"

    @property
    def enabled_flag(self) -> Path:
        return self.state_dir / "enabled"


def load_settings() -> Settings:
    state_dir = Path(os.environ.get("AAW_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()
    cfg = _config_file(state_dir)

    def pick(env: str, key: str, default):
        value = os.environ.get(env)
        if value is not None and value != "":
            return value
        return cfg.get(key, default)

    return Settings(
        state_dir=state_dir,
        relay_url=pick("AAW_RELAY_URL", "relay_url", None),
        computer_name=pick("AAW_COMPUTER_NAME", "computer_name", os.uname().nodename),
        keep_awake=str(pick("AAW_KEEP_AWAKE", "keep_awake", "true")).lower() in ("1", "true", "yes"),
    )
