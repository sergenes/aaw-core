"""Session lifecycle plumbing that can run without tmux: process bookkeeping, pid files,
the known-sessions registry, and the guards of start_session."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from aaw_core.config import Settings
from aaw_core.host import sessions
from aaw_core.host.session_id import Known


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)


def test_processes_lists_this_interpreter():
    procs = sessions._processes()
    assert any(pid == os.getpid() for pid, _ in procs)


def test_alive_and_terminate():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert sessions._alive(p.pid)
        sessions._terminate(p.pid, 2.0)
        p.wait(timeout=5)
        assert not sessions._alive(p.pid)
    finally:
        if p.poll() is None:
            p.kill()


def test_daemon_pid_from_pid_file_and_from_the_process_table(settings, tmp_path):
    assert sessions.daemon_pid(settings, "proj") is None
    settings.run_dir.mkdir()
    settings.daemon_pid_file("proj").write_text("999999")  # stale
    assert sessions.daemon_pid(settings, "proj") is None
    settings.daemon_pid_file("proj").write_text("nonsense")
    assert sessions.daemon_pid(settings, "proj") is None
    # a live process whose command line carries the module and the exact id is found without a pid file
    fake = subprocess.Popen([sys.executable, "-c",
                             "import sys, time; time.sleep(30)", "--", sessions.DAEMON_MODULE,
                             "--project-dir", "/x", "--project-id", "proj", "--agent", "codex"])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and sessions.daemon_pid(settings, "proj") != fake.pid:
            time.sleep(0.1)
        assert sessions.daemon_pid(settings, "proj") == fake.pid
        assert sessions.daemon_pid(settings, "pro") is None  # "pro" never prefix-matches "proj"
        assert sessions.daemon_pid(settings, "proj-codex") is None
        assert sessions.running_daemon_agent(settings, "proj") == "codex"
        assert sessions.stop_daemon(settings, "proj", grace=0.2) == ["  daemon stopped for proj"]
        fake.wait(timeout=5)
    finally:
        if fake.poll() is None:
            fake.kill()
    assert sessions.stop_daemon(settings, "proj") == ["  no daemon running for proj"]


def test_known_sessions_merges_live_tmux_with_stopped_docs(monkeypatch):
    monkeypatch.setattr(sessions, "list_sessions", lambda: [sessions.Session("aaw-proj", "proj", "/home/me/proj", True)])
    monkeypatch.setattr(sessions, "session_agent", lambda p: "claude")
    docs = {"proj": {"project_path": "ENC(/ignored: live wins)", "agent": "codex"},
            "old": {"project_path": "ENC(/home/me/old)", "agent": "gemini"},
            "bad": {"project_path": "ENC(???)"},
            "_supervisor": {"status": "online"}}
    known = sessions.known_sessions(docs, lambda v: "" if "???" in v else v[4:-1])
    assert known == [Known("proj", "/home/me/proj", "claude", True), Known("old", "/home/me/old", "gemini", False),
                     Known("bad", "", None, False)]


def test_start_session_guards(settings, tmp_path):
    with pytest.raises(sessions.SessionError, match="not found"):
        sessions.start_session(settings, tmp_path / "missing", "x", "claude")
    with pytest.raises(sessions.SessionError, match="agent must be"):
        sessions.start_session(settings, tmp_path, "x", "ollama")
    with pytest.raises(sessions.SessionError, match="host is off"):
        sessions.start_session(settings, tmp_path, "x", "claude")


def test_keep_awake_respects_the_setting(settings, monkeypatch):
    monkeypatch.setattr(sessions, "list_sessions", list)
    assert "disabled" in sessions.start_keep_awake(settings)
    assert sessions.stop_keep_awake_if_idle(settings) == "  keep-awake was not running"


def test_log_rotation(settings):
    settings.logs_dir.mkdir()
    log = settings.daemon_log_file("proj")
    log.write_bytes(b"x" * (sessions.LOG_ROTATE_BYTES + 1))
    sessions._rotate_log(log)
    assert not log.exists() and log.with_suffix(".log.1").exists()
