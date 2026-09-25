"""End to end on this machine: a real relay, the real daemon as a subprocess, a real tmux
session running a fake agent, and a simulated phone. Skipped where tmux is missing.

The fake agent is a shell loop that echoes what it is told, so the test proves the host
pipeline (session start, daemon registration, a phone prompt typed into the pane and
echoed to the feed, stop detection) without depending on a real agent's UI."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest
from test_relay import _phone_connect, _poll_until, _recv_until, live_relay  # noqa: F401 (fixture)

from aaw_core.config import load_settings
from aaw_core.encryption import decrypt, load_or_create_key
from aaw_core.hooks.common import tmux_session
from aaw_core.host import sessions, tmux
from aaw_core.host.identity import load_or_create_identity
from aaw_core.transport.base import make_command
from aaw_core.transport.relay import RelayTransport

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

FAKE_AGENT = """#!/usr/bin/env bash
echo "fake claude ready"
while IFS= read -r line; do
  echo "you said: $line"
  [ "$line" = "/exit" ] && exit 0
done
"""
SESSION_ID = "e2e"


@pytest.fixture
def host(tmp_path, monkeypatch, live_relay):  # noqa: F811 - the imported fixture
    # A private tmux server (short socket path: macOS caps it at 104 bytes) so the test
    # never touches the user's own sessions.
    tmux_dir = tempfile.mkdtemp(prefix="aaw-e2e-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", tmux_dir)
    monkeypatch.delenv("TMUX", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_AGENT)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))  # an empty ~/.claude: no --continue
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AAW_RELAY_URL", live_relay)
    monkeypatch.setenv("AAW_KEEP_AWAKE", "false")
    settings = load_settings()
    settings.state_dir.mkdir()
    ident = load_or_create_identity(settings)
    key = load_or_create_key(settings.session_key_file)
    settings.enabled_flag.write_text("1")
    project = tmp_path / "proj"
    project.mkdir()
    try:
        yield settings, ident, key, project, live_relay
    finally:
        tmux.tmux_run(["kill-session", "-t", tmux_session(SESSION_ID)], capture_output=True)
        sessions.stop_daemon(settings, SESSION_ID, grace=0.5)
        tmux.tmux_run(["kill-server"], capture_output=True)
        shutil.rmtree(tmux_dir, ignore_errors=True)


def test_session_bridges_prompts_and_reports_stop(host):
    settings, ident, key, project, relay_url = host
    lines = sessions.start_session(settings, project, SESSION_ID, "claude")
    assert any("daemon started" in ln for ln in lines), lines
    session = tmux_session(SESSION_ID)
    assert tmux.has_session(session)
    assert sessions.session_agent(SESSION_ID) == "claude"
    assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)

    reader = RelayTransport(relay_url=relay_url, token=ident.token, computer_id=ident.computer_id,
                            project_id="_test", sessions_dir=settings.sessions_dir, enc_key=key).start()
    try:
        assert reader.wait_connected(10)

        # the daemon registers the project on the relay, with its path encrypted
        def registered():
            doc = reader.list_projects().get(SESSION_ID) or {}
            return doc if doc.get("status") in ("running", "idle") and doc.get("project_path") else None
        doc = _poll_until(registered, timeout=30)
        assert doc["agent"] == "claude"
        assert decrypt(doc["project_path"], key) == str(project)
        assert sessions.daemon_pid(settings, SESSION_ID)

        # a prompt from the phone is typed into the pane, consumed, and echoed to the feed
        phone = _phone_connect(relay_url, reader.register_phone_token())
        phone.send(json.dumps({"type": "subscribe", "project_id": SESSION_ID}))
        cmd = make_command("hello from the phone", key, source="phone")
        phone.send(json.dumps({"type": "command", "project_id": SESSION_ID, "command": cmd}))
        assert _poll_until(lambda: "you said: hello from the phone" in tmux.visible_pane(session), timeout=30)
        # the daemon echoes the prompt to the feed first, then marks the command consumed
        ev = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "message", timeout=30)
        assert decrypt(ev["event"]["payload"]["content"], key) == "hello from the phone"
        upd = _recv_until(phone, lambda f: f.get("type") == "command_update" and f["command_id"] == cmd["id"],
                          timeout=30)
        assert upd["fields"]["consumed"] is True
        local = (settings.sessions_dir / f"{SESSION_ID}.jsonl").read_text()
        assert '"content": "hello from the phone"' in local  # plaintext stays on the computer

        # the agent goes away: the daemon writes "stopped" on its own and exits
        tmux.tmux_run(["kill-session", "-t", session], capture_output=True)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status") == "stopped",
                           timeout=45)
        assert _poll_until(lambda: sessions.daemon_pid(settings, SESSION_ID) is None, timeout=30)
        phone.close()
    finally:
        reader.stop(flush_timeout=0)
