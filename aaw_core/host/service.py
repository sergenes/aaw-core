"""The supervisor as a user service: a systemd user unit on Linux, a launchd agent on macOS.

Both run ``aaw supervisor`` at login and keep it alive. The unit is generated with the
absolute path of this very ``aaw`` executable and the AAW_* variables present at
install time, so a custom state dir or relay url set in the shell survives into the
service; settings in ``<state dir>/config.json`` need no variables at all.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aaw_core.config import Settings

SYSTEMD_UNIT = "aaw-supervisor"
LAUNCHD_LABEL = "app.agentsatwork.core.supervisor"


def is_macos() -> bool:
    return sys.platform == "darwin"


def aaw_executable() -> str:
    """The `aaw` console script next to this interpreter, else whatever is on PATH."""
    candidate = Path(sys.executable).parent / "aaw"
    if candidate.exists():
        return str(candidate)
    return shutil.which("aaw") or "aaw"


def _aaw_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith("AAW_") and v}


def unit_path() -> Path:
    if is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"


def render_systemd_unit(exe: str, env: dict[str, str]) -> str:
    lines = [
        "[Unit]",
        "Description=Agents At Work Core supervisor (session watchdogs, heartbeat, remote start)",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exe} supervisor",
        "Restart=always",
        "RestartSec=5",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    lines += [f"Environment={k}={v}" for k, v in sorted(env.items())]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def render_launchd_plist(exe: str, env: dict[str, str], logs_dir: Path) -> bytes:
    # launchd starts agents with a minimal PATH; the supervisor extends it, but the tmux
    # server it spawns inherits this one, so hand over the installing shell's PATH too.
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"}
    environment.update(env)
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [exe, "supervisor"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "EnvironmentVariables": environment,
        "StandardOutPath": str(logs_dir / "supervisor.launchd.log"),
        "StandardErrorPath": str(logs_dir / "supervisor.launchd.log"),
    }
    return plistlib.dumps(plist)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _launchd_target() -> str:
    return f"gui/{os.getuid()}"


def install(settings: Settings) -> list[str]:
    """Write the unit and start it now. Returns the log lines."""
    exe, env = aaw_executable(), _aaw_env()
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    out = []
    if is_macos():
        if not shutil.which("launchctl"):
            return ["launchctl not found; run `aaw supervisor` yourself"]
        label = f"{_launchd_target()}/{LAUNCHD_LABEL}"
        _run(["launchctl", "bootout", label])  # replace a previous one
        # launchd unloads asynchronously: a bootstrap right after the bootout fails with
        # "Input/output error" while the old definition is still on its way out. Wait for
        # the label to disappear, then load, retrying for a few seconds.
        for _ in range(20):
            if _run(["launchctl", "print", label]).returncode != 0:
                break
            time.sleep(0.25)
        path.write_bytes(render_launchd_plist(exe, env, settings.logs_dir))
        out.append(f"wrote {path}")
        r = None
        for _ in range(20):
            r = _run(["launchctl", "bootstrap", _launchd_target(), str(path)])
            if r.returncode == 0:
                break
            time.sleep(0.5)
        if r is None or r.returncode != 0:
            # The legacy API tolerates the in-between state; try it before giving up.
            r = _run(["launchctl", "load", "-w", str(path)])
        if r.returncode != 0:
            out.append(f"launchctl bootstrap failed: {(r.stderr or r.stdout).strip()}")
        else:
            out.append("supervisor service started (launchd); it starts again at every login")
        return out
    if not shutil.which("systemctl") or _run(["systemctl", "--user", "show-environment"]).returncode != 0:
        return ["no systemd user session here; run `aaw supervisor` yourself (or under your own init)"]
    path.write_text(render_systemd_unit(exe, env))
    out.append(f"wrote {path}")
    _run(["systemctl", "--user", "daemon-reload"])
    r = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
    if r.returncode != 0:
        out.append(f"systemctl enable failed: {(r.stderr or r.stdout).strip()}")
    else:
        out.append(f"supervisor service started (systemd --user); status: systemctl --user status {SYSTEMD_UNIT}")
        out.append("to keep it running after logout on a server: loginctl enable-linger $USER")
    return out


def stop() -> str:
    if is_macos():
        if not shutil.which("launchctl"):
            return "  no launchctl; nothing to stop"
        r = _run(["launchctl", "bootout", f"{_launchd_target()}/{LAUNCHD_LABEL}"])
        return "  supervisor service stopped" if r.returncode == 0 else "  supervisor service: not running"
    if not shutil.which("systemctl"):
        return "  no systemctl; nothing to stop"
    r = _run(["systemctl", "--user", "stop", SYSTEMD_UNIT])
    return "  supervisor service stopped" if r.returncode == 0 else "  supervisor service: not running"


def start() -> str:
    if is_macos():
        path = unit_path()
        if not path.exists():
            return "  no service installed (aaw service install)"
        r = _run(["launchctl", "bootstrap", _launchd_target(), str(path)])
        return "  supervisor service started" if r.returncode == 0 else "  supervisor service: already running"
    if not shutil.which("systemctl"):
        return "  no systemctl; run `aaw supervisor` yourself"
    r = _run(["systemctl", "--user", "start", SYSTEMD_UNIT])
    return "  supervisor service started" if r.returncode == 0 else f"  {(r.stderr or '').strip() or 'not installed'}"


def uninstall() -> list[str]:
    out = [stop()]
    path = unit_path()
    if path.exists():
        path.unlink()
        out.append(f"  removed {path}")
        if not is_macos() and shutil.which("systemctl"):
            _run(["systemctl", "--user", "daemon-reload"])
    return out


def status() -> str:
    path = unit_path()
    if not path.exists():
        return "not installed"
    if is_macos():
        r = _run(["launchctl", "print", f"{_launchd_target()}/{LAUNCHD_LABEL}"])
        return "installed, running (launchd)" if r.returncode == 0 else "installed, not running (launchd)"
    if not shutil.which("systemctl"):
        return "installed (no systemctl to ask)"
    r = _run(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
    return f"installed, {r.stdout.strip() or 'unknown'} (systemd --user)"
