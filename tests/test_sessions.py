"""Session lifecycle plumbing that can run without tmux: process bookkeeping, pid files,
the known-sessions registry, and the guards of start_session."""

from __future__ import annotations

import json
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


def test_list_sessions_counts_any_number_of_windows_as_attached(monkeypatch):
    """#{session_attached} is the number of clients, not a flag: until 0.1.20 a session
    open in two terminal windows read as detached."""
    out = "aaw-one|1|/p/one\naaw-two|2|/p/two\naaw-none|0|/p/none\nother|1|/p/other\n"
    monkeypatch.setattr(sessions.tmux, "tmux_run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    assert {s.project: s.attached for s in sessions.list_sessions()} == {"one": True, "two": True, "none": False}


# ── a newer scoot on PyPI ────────────────────────────────────────────────────


class _PyPI:
    """urlopen stand-in that answers with a version and counts the calls."""

    def __init__(self, version="0.16.0", fail=False, url=None):
        self.version, self.fail, self.calls = version, fail, 0
        self.url = url or sessions.SCOOT_PYPI_URL

    def __call__(self, url, timeout=None):
        import io
        self.calls += 1
        assert url == self.url
        if self.fail:
            raise OSError("offline")
        return io.BytesIO(json.dumps({"info": {"version": self.version}}).encode())


def _scoot_on_path(monkeypatch, tmp_path, version="0.15.0", body="#!/usr/bin/env python3\n"):
    exe = tmp_path / "bin" / "scoot"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(body)
    monkeypatch.setattr(sessions.shutil, "which", lambda name: str(exe) if name == "scoot" else None)
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, f"scoot {version}\n", ""))
    return exe


def test_scoot_update_is_offered_when_pypi_has_a_newer_one(tmp_path, monkeypatch, settings):
    import urllib.request
    monkeypatch.delenv("AAW_NO_UPDATE_CHECK")
    pypi = _PyPI("0.16.0")
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    _scoot_on_path(monkeypatch, tmp_path, "0.15.0")
    update = sessions.scoot_update(settings)
    assert update == {"installed": "0.15.0", "latest": "0.16.0", "command": "python3 -m pip install --upgrade scootcli"}
    # asked once a day: the second look reads the remembered answer
    assert sessions.scoot_update(settings) == update and pypi.calls == 1
    # up to date, ahead of PyPI (a checkout), or a dev build: nothing to offer
    for version in ("0.16.0", "0.17.0", "0.16.0.dev3", "unknown"):
        _scoot_on_path(monkeypatch, tmp_path, version)
        assert sessions.scoot_update(settings) is None, version
    assert pypi.calls == 1


def test_the_engine_offers_its_own_update_with_the_command_for_its_install(tmp_path, monkeypatch, settings):
    """Nothing told a Linux user that aaw-core had a new release (the Mac app and the
    phones learn it from the site). `aaw status` says so, from PyPI, once a day."""
    import urllib.request
    monkeypatch.delenv("AAW_NO_UPDATE_CHECK")
    pypi = _PyPI("0.1.27", url=sessions.ENGINE_PYPI_URL)
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    monkeypatch.setattr(sessions.Path, "home", lambda: tmp_path)
    venv = tmp_path / ".local" / "share" / "aaw-core" / "venv"
    venv.mkdir(parents=True)
    monkeypatch.setattr(sessions.sys, "prefix", str(venv))
    assert sessions.engine_update(settings, "0.1.26") == {
        "installed": "0.1.26", "latest": "0.1.27", "command": sessions.ENGINE_INSTALLER}
    assert sessions.engine_update(settings, "0.1.26") is not None and pypi.calls == 1  # remembered for a day
    assert (settings.state_dir / "engine_latest.json").exists()
    assert not (settings.state_dir / "scoot_latest.json").exists()  # each package keeps its own answer
    for version in ("0.1.27", "0.2.0", "0.1.27.dev1", ""):  # current, ahead (a checkout), a dev build
        assert sessions.engine_update(settings, version) is None, version
    assert pypi.calls == 1


def test_the_engine_update_command_follows_how_it_was_installed(tmp_path, monkeypatch, settings):
    import urllib.request
    monkeypatch.setattr(sessions.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sessions.sys, "executable", "/opt/py/bin/python3")
    assert sessions.engine_update_command("/data/aaw-core/venv") == sessions.ENGINE_INSTALLER  # XDG_DATA_HOME
    assert sessions.engine_update_command("/home/u/.local/pipx/venvs/aaw-core") == "pipx upgrade aaw-core"
    assert sessions.engine_update_command("/opt/py") == "/opt/py/bin/python3 -m pip install --upgrade aaw-core"
    # inside the Mac app the app replaces the engine: no offer, and PyPI is never asked
    mac = tmp_path / "Library" / "Application Support" / "AgentBridge" / "python"
    mac.mkdir(parents=True)
    assert sessions.engine_update_command(str(mac)) == ""
    monkeypatch.delenv("AAW_NO_UPDATE_CHECK")
    pypi = _PyPI("9.9.9", url=sessions.ENGINE_PYPI_URL)
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    monkeypatch.setattr(sessions.sys, "prefix", str(mac))
    assert sessions.engine_update(settings, "0.1.26") is None and pypi.calls == 0


def test_scoot_update_stays_quiet_offline_and_when_switched_off(tmp_path, monkeypatch, settings):
    import urllib.request
    pypi = _PyPI(fail=True)
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    _scoot_on_path(monkeypatch, tmp_path, "0.15.0")
    assert sessions.scoot_update(settings) is None and pypi.calls == 0  # AAW_NO_UPDATE_CHECK=1
    monkeypatch.delenv("AAW_NO_UPDATE_CHECK")
    assert sessions.scoot_update(settings) is None and pypi.calls == 1  # offline
    assert sessions.scoot_update(settings) is None and pypi.calls == 1  # and not asked again at once
    # an hour later it asks again, and a network that is back answers
    pypi.fail = False
    assert sessions.scoot_latest_version(settings, now=time.time() + sessions.SCOOT_CHECK_RETRY_S + 1) == "0.16.0"
    assert pypi.calls == 2
    # 0.9 is older than 0.15: versions compare as numbers, not as text
    assert sessions.version_tuple("0.9.0") < sessions.version_tuple("scoot 0.15.0")


def test_scoot_update_command_follows_how_scoot_was_installed(tmp_path, monkeypatch):
    import zipfile
    exe = _scoot_on_path(monkeypatch, tmp_path, body="#!/opt/miniconda3/bin/python\nimport scootcli\n")
    assert sessions.scoot_update_command() == "/opt/miniconda3/bin/python -m pip install --upgrade scootcli"
    exe.write_text("#!/bin/sh\nexec something\n")
    assert sessions.scoot_update_command() == "pip install --upgrade scootcli"
    with zipfile.ZipFile(exe, "w") as z:  # the one-line installer puts a zipapp on PATH
        z.writestr("__main__.py", "")
    assert sessions.scoot_update_command() == sessions.SCOOT_INSTALLER
    venv = tmp_path / "pipx" / "venvs" / "scootcli" / "bin"
    venv.mkdir(parents=True)
    (venv / "scoot").write_text("#!/x/python\n")
    exe.unlink()
    exe.symlink_to(venv / "scoot")
    assert sessions.scoot_update_command() == "pipx upgrade scootcli"


# ── Ollama for a scoot session (0.1.22) ──────────────────────────────────────


def _ollama(monkeypatch, *, installed=True, answering=False, launches=True, comes_up=True):
    from aaw_core.host import ollama
    calls = []
    monkeypatch.setattr(ollama, "installed", lambda: installed)
    monkeypatch.setattr(ollama, "answering", lambda timeout=2.0: answering)
    monkeypatch.setattr(ollama, "launch", lambda log: calls.append("launch") or launches)
    monkeypatch.setattr(ollama, "wait_until_answering", lambda wait_s=0: comes_up)
    return ollama, calls


def _statuses(*answers):
    left = list(answers)
    return lambda: left.pop(0) if len(left) > 1 else left[0]


DOWN = ([], {"ollama": "could not connect"})
UP = (["ollama/qwen:7b"], {})


def test_ollama_is_left_alone_unless_an_ollama_model_is_asked_for(monkeypatch, tmp_path):
    ollama, calls = _ollama(monkeypatch)
    never = lambda: 1 / 0
    assert ollama.ready_for(None, never, tmp_path / "o.log") == []
    assert ollama.ready_for("openai/gpt-5", never, tmp_path / "o.log") == []
    assert ollama.ready_for("ollama/qwen:7b", lambda: UP, tmp_path / "o.log") == []
    assert ollama.ready_for("ollama/qwen:7b", lambda: None, tmp_path / "o.log") == []  # scoot unreadable
    assert calls == []


def test_ollama_is_started_when_installed_and_down(monkeypatch, tmp_path):
    ollama, calls = _ollama(monkeypatch)
    lines = ollama.ready_for("ollama/qwen:7b", _statuses(DOWN, UP), tmp_path / "o.log")
    assert lines == ["  Ollama was not running; started it"] and calls == ["launch"]


def test_ollama_says_why_it_cannot_serve(monkeypatch, tmp_path):
    import pytest
    log = tmp_path / "o.log"
    ollama, calls = _ollama(monkeypatch, installed=False)
    with pytest.raises(ollama.NotReady, match="not installed"):
        ollama.ready_for("ollama/qwen:7b", lambda: DOWN, log)
    assert calls == []
    ollama, _ = _ollama(monkeypatch, comes_up=False)
    with pytest.raises(ollama.NotReady, match="did not start"):
        ollama.ready_for("ollama/qwen:7b", lambda: DOWN, log)
    ollama, _ = _ollama(monkeypatch, launches=False)
    with pytest.raises(ollama.NotReady, match="did not start"):
        ollama.ready_for("ollama/qwen:7b", lambda: DOWN, log)
    ollama, calls = _ollama(monkeypatch, answering=True)  # up, the model is not pulled
    with pytest.raises(ollama.NotReady, match="ollama pull codestral:22b"):
        ollama.ready_for("ollama/codestral:22b", lambda: UP, log)
    assert calls == []
    ollama, _ = _ollama(monkeypatch)  # started, the model is still not there
    with pytest.raises(ollama.NotReady, match="ollama pull codestral:22b"):
        ollama.ready_for("ollama/codestral:22b", _statuses(DOWN, UP), log)
    ollama, _ = _ollama(monkeypatch, answering=True)  # scoot looks at another address
    with pytest.raises(ollama.NotReady, match="scoot cannot reach it"):
        ollama.ready_for("ollama/qwen:7b", lambda: DOWN, log)


def test_scoot_saved_model_is_the_newest_session_of_the_folder(monkeypatch, tmp_path):
    import json
    import os

    from aaw_core.host.detect import scoot_saved_model
    monkeypatch.setenv("SCOOT_STATE_DIR", str(tmp_path))
    proj = tmp_path / "proj"
    assert scoot_saved_model(proj) == ""
    d = tmp_path / "sessions"
    d.mkdir()
    for i, (root, model) in enumerate([(proj, "ollama/old"), (proj, "ollama/new"), (tmp_path / "other", "openai/x")]):
        f = d / f"{i}.json"
        f.write_text(json.dumps({"root": str(root), "active_model": model}))
        os.utime(f, (1000 + i, 1000 + i))
    (d / "bad.json").write_text("{")
    assert scoot_saved_model(proj) == "ollama/new"


def test_start_ollama_on_request(monkeypatch, tmp_path):
    log = tmp_path / "o.log"
    ollama, calls = _ollama(monkeypatch, answering=True)
    assert ollama.start(log) == "running" and calls == []
    ollama, calls = _ollama(monkeypatch, installed=False)
    assert ollama.start(log) == "not_installed" and calls == []
    ollama, calls = _ollama(monkeypatch)
    assert ollama.start(log) == "started" and calls == ["launch"]
    ollama, _ = _ollama(monkeypatch, comes_up=False)
    assert ollama.start(log) == "failed"

