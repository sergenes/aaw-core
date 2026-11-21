"""Ollama, for a scoot session that asks for one of its models.

A session on an `ollama/...` model cannot answer while Ollama is stopped, and scoot only
says the model is unknown. So before such a session starts, the host starts Ollama when it
is installed and not running, and says plainly when it is not installed or lacks the model.
Ollama is started only for a session that asks for it, never from a heartbeat: it holds a
lot of memory."""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LOCAL_URL = "http://127.0.0.1:11434/api/version"
START_WAIT_S = 20.0
POLL_S = 0.5
MAC_APPS = (Path("/Applications/Ollama.app"), Path.home() / "Applications/Ollama.app")
PREFIX = "ollama/"


class NotReady(Exception):
    """Ollama cannot serve the model; the message is for the user."""


def mac_app() -> Path | None:
    if sys.platform != "darwin":
        return None
    return next((p for p in MAC_APPS if p.exists()), None)


def installed() -> bool:
    return bool(shutil.which("ollama")) or mac_app() is not None


def answering(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(LOCAL_URL, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001  (refused, timed out, not Ollama: all "not answering")
        return False


def _run_ok(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def launch(log_path: Path) -> bool:
    """Ask Ollama to start, the way this machine runs it: the Mac app, a systemd unit, or
    `ollama serve` left running on its own. True when something was started."""
    app = mac_app()
    if app is not None:
        return _run_ok(["open", "-g", "-a", str(app)])  # -g: not brought to the front
    if not shutil.which("ollama"):
        return False
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        # A user unit, then the installer's system unit (works only where no password is asked).
        if _run_ok(["systemctl", "--user", "start", "ollama"]):
            return True
        if _run_ok(["systemctl", "--no-ask-password", "start", "ollama"]):
            return True
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            subprocess.Popen(["ollama", "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    except OSError:
        return False
    return True


def wait_until_answering(wait_s: float = START_WAIT_S) -> bool:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if answering(timeout=1.0):
            return True
        time.sleep(POLL_S)
    return answering(timeout=1.0)


def start(log_path: Path) -> str:
    """Start Ollama because the user asked (the Mac app's and the phones' "Start Ollama"):
    "running" when it already answers, "started", "not_installed", or "failed"."""
    if answering():
        return "running"
    if not installed():
        return "not_installed"
    return "started" if launch(log_path) and wait_until_answering() else "failed"


def ready_for(model: str | None, scoot_status, log_path: Path) -> list[str]:
    """Make sure scoot can serve `model` when it is an Ollama model. `scoot_status` is
    sessions.scoot_status: scoot itself is the judge of what it can reach. Returns the
    lines to show (empty when nothing had to be done); raises NotReady."""
    if not model or not model.startswith(PREFIX):
        return []
    name = model[len(PREFIX):]
    status = scoot_status()
    if status is None:
        return []  # scoot is absent or unreadable: the session start reports that itself
    models, errors = status
    if model in models:
        return []
    no_model = NotReady(f"Ollama has no model {name}. Pull it with: ollama pull {name}")
    if "ollama" not in errors:
        raise no_model
    if not installed():
        raise NotReady("Ollama is not installed on this computer. Get it from https://ollama.com, "
                       "or pick a cloud model (`aaw models`).")
    if not answering():
        if not launch(log_path) or not wait_until_answering():
            raise NotReady("Ollama is installed but did not start. Start it by hand, then try again.")
        started = ["  Ollama was not running; started it"]
    else:
        started = []
    status = scoot_status()
    models, errors = status if status is not None else ([], {})
    if model in models:
        return started
    if "ollama" in errors:
        raise NotReady(f"Ollama is running, but scoot cannot reach it: {errors['ollama']}")
    raise no_model
