"""The computer name default: the Computer Name on macOS, the hostname elsewhere."""

import json
import subprocess

from aaw_core import config
from aaw_core.host.identity import load_identity


class _Uname:
    def __init__(self, nodename):
        self.nodename = nodename


def test_linux_strips_local_suffix(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "uname", lambda: _Uname("rocky.local"))
    assert config.default_computer_name() == "rocky"


def test_linux_plain_hostname(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "uname", lambda: _Uname("rocky"))
    assert config.default_computer_name() == "rocky"


def test_macos_uses_computer_name(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="Sergey's MacBook Pro\n"))
    assert config.default_computer_name() == "Sergey's MacBook Pro"


def test_macos_falls_back_to_hostname(monkeypatch):
    def fail(*a, **k):
        raise OSError("no scutil")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.subprocess, "run", fail)
    monkeypatch.setattr(config.os, "uname", lambda: _Uname("Sergeys-MacBook-Pro.local"))
    assert config.default_computer_name() == "Sergeys-MacBook-Pro"


def test_configured_name_wins_and_skips_the_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AAW_COMPUTER_NAME", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"computer_name": "studio"}))
    monkeypatch.setattr(config, "default_computer_name", lambda: (_ for _ in ()).throw(AssertionError))
    assert config.load_settings().computer_name == "studio"
    monkeypatch.setenv("AAW_COMPUTER_NAME", "desk")
    assert config.load_settings().computer_name == "desk"


def test_identity_follows_the_current_name(monkeypatch, tmp_path):
    """A host set up with the old hostname default shows the current name, no re-link."""
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AAW_COMPUTER_NAME", "Sergey's MacBook Pro")
    settings = config.load_settings()
    settings.host_file.write_text(json.dumps(
        {"computer_id": "c1", "token": "t1", "computer_name": "Sergeys-MacBook-Pro.local"}))
    assert load_identity(settings).computer_name == "Sergey's MacBook Pro"
