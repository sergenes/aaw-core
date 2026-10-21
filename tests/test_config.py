"""The computer name default: the Computer Name on macOS, the hostname elsewhere."""

import dataclasses
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


def test_identity_prefers_the_saved_name_over_the_hostname_fallback_on_macos(monkeypatch, tmp_path):
    """Under load the macOS name lookup can time out and fall back to the hostname. Ten
    daemons each resolving the name on their own then wrote "Serges-m1-MacBook-Pro" while
    the supervisor wrote "Serge's m1 MacBook Pro", and the phones flipped between the two.
    The name saved at setup beats the hostname; a real name, or a Linux hostname, stands."""
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AAW_COMPUTER_NAME", raising=False)
    monkeypatch.setattr(config.os, "uname", lambda: _Uname("Sergeys-MacBook-Pro.local"))
    settings = config.load_settings()
    host = settings.host_file
    host.write_text(json.dumps({"computer_id": "c1", "token": "t1", "computer_name": "Sergey's MacBook Pro"}))
    from aaw_core.host import identity
    monkeypatch.setattr(identity.sys, "platform", "darwin")
    fallback = dataclasses.replace(settings, computer_name="Sergeys-MacBook-Pro")  # what the fallback produced
    assert load_identity(fallback).computer_name == "Sergey's MacBook Pro"
    real = dataclasses.replace(settings, computer_name="Studio Mac")  # the lookup worked: the current name wins
    assert load_identity(real).computer_name == "Studio Mac"
    monkeypatch.setattr(identity.sys, "platform", "linux")
    assert load_identity(fallback).computer_name == "Sergeys-MacBook-Pro"  # on Linux the hostname is the name

