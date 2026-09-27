"""The service units: rendered with this executable and the AAW_* environment."""

from __future__ import annotations

import plistlib
from pathlib import Path

from aaw_core.host import service


def test_systemd_unit_carries_the_executable_and_env():
    text = service.render_systemd_unit("/opt/venv/bin/aaw", {"AAW_STATE_DIR": "/srv/aaw", "AAW_RELAY_URL": "wss://r"})
    assert "ExecStart=/opt/venv/bin/aaw supervisor" in text
    assert "Environment=AAW_RELAY_URL=wss://r\nEnvironment=AAW_STATE_DIR=/srv/aaw" in text
    assert "WantedBy=default.target" in text and "Restart=always" in text


def test_launchd_plist_is_valid_and_keeps_alive(tmp_path):
    raw = service.render_launchd_plist("/opt/venv/bin/aaw", {"AAW_RELAY_URL": "wss://r"}, tmp_path)
    plist = plistlib.loads(raw)
    assert plist["Label"] == service.LAUNCHD_LABEL
    assert plist["ProgramArguments"] == ["/opt/venv/bin/aaw", "supervisor"]
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["EnvironmentVariables"]["AAW_RELAY_URL"] == "wss://r"
    assert "PATH" in plist["EnvironmentVariables"]
    assert plist["StandardOutPath"] == str(tmp_path / "supervisor.launchd.log")


def test_unit_path_is_per_platform(monkeypatch):
    monkeypatch.setattr(service, "is_macos", lambda: True)
    assert service.unit_path() == Path.home() / "Library/LaunchAgents" / f"{service.LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(service, "is_macos", lambda: False)
    assert service.unit_path() == Path.home() / ".config/systemd/user" / f"{service.SYSTEMD_UNIT}.service"


def test_only_aaw_variables_are_captured(monkeypatch):
    monkeypatch.setenv("AAW_RELAY_URL", "wss://r")
    monkeypatch.setenv("AAW_EMPTY", "")
    monkeypatch.setenv("OTHER", "x")
    assert service._aaw_env() == {"AAW_RELAY_URL": "wss://r"}


def test_status_and_uninstall_without_a_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "missing.service")
    monkeypatch.setattr(service, "is_macos", lambda: False)
    assert service.status() == "not installed"
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    assert service.uninstall() == ["  no systemctl; nothing to stop"]


def test_start_hint_names_the_service_to_install_or_start(monkeypatch, tmp_path):
    unit = tmp_path / "aaw-supervisor.service"
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    assert "aaw service install" in service.start_hint()
    unit.write_text("[Unit]")
    assert "aaw service start" in service.start_hint()


def test_linger_off_reads_loginctl(monkeypatch):
    import subprocess
    monkeypatch.setattr(service, "is_macos", lambda: False)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)
    for answer, off in (("Linger=no\n", True), ("Linger=yes\n", False)):
        monkeypatch.setattr(service, "_run", lambda args, a=answer: subprocess.CompletedProcess(args, 0, stdout=a))
        assert service.linger_off() is off
