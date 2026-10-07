"""The supervisor's decisions, with the relay and tmux stubbed out."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from aaw_core.config import Settings
from aaw_core.encryption import decrypt, encrypt, generate_key_b64
from aaw_core.host import fs, hooks_installer, sessions
from aaw_core.host.supervisor import STOPPED_STREAK, Supervisor

KEY = generate_key_b64()


class FakeTransport:
    computer_id = "c1"

    def __init__(self, projects=None):
        self.projects = projects or {}
        self.computer_updates: list[dict] = []
        self.project_writes: list[tuple[str, dict]] = []
        self.commands: dict[str, list] = {}

    def update_computer(self, **fields):
        self.computer_updates.append(fields)

    def list_projects(self):
        return self.projects

    def list_commands(self, project_id):
        return self.commands.get(project_id, [])

    def set_project_fields(self, project_id, fields):
        self.project_writes.append((project_id, fields))

    def mirror_projects(self, docs):
        self.mirrored = docs


@pytest.fixture
def sup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    settings = Settings(state_dir=tmp_path / "state", relay_url="ws://x", computer_name="box", keep_awake=False,
                        browse_roots=(str(home),))
    settings.state_dir.mkdir()
    settings.session_key_file.write_text(KEY)
    s = Supervisor(settings, transport=FakeTransport())
    s.home = home
    monkeypatch.setattr(sessions, "list_sessions", list)
    monkeypatch.setattr(sessions, "session_agent", lambda p: None)
    monkeypatch.setattr(sessions, "scoot_models", lambda: None)
    monkeypatch.setattr(hooks_installer, "detected_agents", lambda: ["claude", "codex"])
    return s


def test_fs_browse_answers_encrypted_and_fenced(sup):
    (sup.home / "proj").mkdir()
    reply = sup.handle_request("fs_browse", {})
    assert reply["error"] == "" and reply["at_root"] and reply["parent_enc"] is None
    assert decrypt(reply["resolved_path_enc"], KEY) == fs.canon(str(sup.home))
    assert decrypt(reply["entries"][0]["name_enc"], KEY) == "proj"
    outside = sup.handle_request("fs_browse", {"path_enc": encrypt("/", KEY)})
    assert outside["error"] == fs.ERR_OUTSIDE_ROOTS
    foreign = sup.handle_request("fs_browse", {"path_enc": encrypt("/x", generate_key_b64())})
    assert foreign["error"] == fs.ERR_DENIED  # undecryptable is never treated as "home"


def test_fs_fetch_round_trip(sup):
    (sup.home / "a.md").write_text("hello")
    reply = sup.handle_request("fs_fetch", {"path_enc": encrypt(str(sup.home / "a.md"), KEY)})
    assert reply["error"] == "" and reply["mime"] == "text/markdown" and reply["total_chunks"] == 1
    assert base64.b64decode(decrypt(reply["chunks"][0], KEY)) == b"hello"
    assert sup.handle_request("fs_fetch", {})["error"] == fs.ERR_DENIED


def test_fs_attach_saves_the_image_and_returns_its_path(sup):
    img = b"\xff\xd8\xff" + b"photo" * 50
    chunks = [encrypt(base64.b64encode(img[i:i + 16]).decode(), KEY) for i in range(0, len(img), 16)]
    reply = sup.handle_request("fs_attach", {"chunks": chunks, "suffix": "jpg"})
    assert reply["error"] == "" and reply["size"] == len(img)
    path = decrypt(reply["path_enc"], KEY)
    assert path.startswith(str(sup.settings.attachments_dir)) and path.endswith(".jpg")
    with open(path, "rb") as f:
        assert f.read() == img
    # an undecryptable chunk (wrong key) is a denied request, never a crash
    bad = sup.handle_request("fs_attach", {"chunks": [encrypt("x", generate_key_b64())], "suffix": "jpg"})
    assert bad["error"] == fs.ERR_DENIED


def test_unknown_request_and_no_key(sup):
    assert sup.handle_request("bogus", {}) == {"error": "unknown_request"}
    sup.enc_key = None
    assert sup.handle_request("fs_browse", {})["error"] == fs.ERR_DENIED


def test_new_session_resolves_conflicts_and_launches(sup, monkeypatch):
    proj = sup.home / "proj"
    proj.mkdir()
    launched = []
    monkeypatch.setattr(sessions, "start_session",
                        lambda settings, path, pid, agent, model=None: launched.append((str(path), pid, agent, model))
                        or ["ok"])
    enc = encrypt(str(proj), KEY)
    assert sup.handle_request("new_session", {"path_enc": encrypt("/nope", KEY)})["result"] == "outside_roots"
    assert sup.handle_request("new_session", {"path_enc": encrypt(str(sup.home / "zz"), KEY)})["result"] == "not_found"
    assert sup.handle_request("new_session", {"path_enc": enc, "agent": "grok"})["result"] == "agent_unavailable"
    assert sup.handle_request("new_session", {"path_enc": enc, "agent": "codex"}) == {
        "result": "started", "session_id": "proj"}
    assert launched == [(fs.canon(str(proj)), "proj", "codex", None)]

    # another agent live on the folder: conflict unless the phone said "parallel"
    monkeypatch.setattr(sessions, "list_sessions",
                        lambda: [sessions.Session("aaw-proj", "proj", fs.canon(str(proj)), False)])
    monkeypatch.setattr(sessions, "session_agent", lambda p: "codex")
    reply = sup.handle_request("new_session", {"path_enc": enc, "agent": "claude"})
    assert reply == {"result": "conflict", "conflict_agents": ["codex"], "conflict_session_id": "proj"}
    reply = sup.handle_request("new_session", {"path_enc": enc, "agent": "claude", "intent": "parallel"})
    assert reply == {"result": "started", "session_id": "proj-claude"}
    # the same agent live on the folder simply attaches
    assert sup.handle_request("new_session", {"path_enc": enc, "agent": "codex"}) == {
        "result": "started", "session_id": "proj"}


def test_start_session_request_uses_the_stopped_doc(sup, monkeypatch):
    proj = sup.home / "proj"
    proj.mkdir()
    sup.transport.projects = {"proj": {"status": "stopped", "agent": "claude", "project_path": encrypt(str(proj), KEY)},
                              "gone": {"status": "stopped", "agent": "claude",
                                       "project_path": encrypt(str(sup.home / "gone"), KEY)}}
    launched = []
    monkeypatch.setattr(sessions, "start_session",
                        lambda settings, path, pid, agent, model=None: launched.append((pid, agent)) or [])
    monkeypatch.setattr(sessions, "session_alive", lambda p: False)
    assert sup.handle_request("start_session", {"project_id": "proj"}) == {"result": "started", "session_id": "proj"}
    assert launched == [("proj", "claude")]
    assert sup.handle_request("start_session", {"project_id": "gone"})["result"] == "not_found"
    pid, fields = sup.transport.project_writes[-1]
    assert pid == "gone" and fields["status"] == "stopped" and "Folder not found" in decrypt(
        fields["last_event_summary"], KEY)
    assert sup.handle_request("start_session", {"project_id": "nope"})["result"] == "not_found"
    # a failed launch is an error result, never an exception
    monkeypatch.setattr(sessions, "start_session",
                        lambda *a, **k: (_ for _ in ()).throw(sessions.SessionError("tmux missing")))
    assert sup.handle_request("start_session", {"project_id": "proj"}) == {"result": "error"}


def test_reconcile_writes_stopped_after_a_streak(sup):
    sup.transport.projects = {"proj": {"status": "running", "agent": "claude"},
                              "done": {"status": "stopped"}, "_supervisor": {}}
    sup.refresh_cache()
    assert set(sup.cache) == {"proj", "done"}
    for _ in range(STOPPED_STREAK - 1):
        sup.reconcile_stopped()
    assert sup.transport.project_writes == []
    sup.reconcile_stopped()
    assert sup.transport.project_writes == [("proj", {"status": "stopped", "last_event_ts":
                                                       sup.transport.project_writes[0][1]["last_event_ts"],
                                                       "pending_question_id": ""})]
    assert sup.cache["proj"]["status"] == "stopped"


def test_restart_stuck_only_for_old_unconsumed_prompts(sup, monkeypatch):
    import time
    monkeypatch.setattr(sessions, "list_sessions", lambda: [sessions.Session("aaw-p", "p", "/tmp", False)])
    restarted = []
    monkeypatch.setattr(sessions, "start_daemon", lambda s, d, pid, agent: restarted.append(pid) or [])
    now_ms = int(time.time() * 1000)
    sup.transport.commands = {"p": [{"type": "command", "ts": now_ms - 10_000},
                                    {"type": "command", "ts": now_ms - 600_000, "deliver_at": now_ms + 1}]}
    sup.restart_stuck()
    assert restarted == []  # a fresh prompt and a scheduled one are not "stuck"
    sup.transport.commands = {"p": [{"type": "command", "ts": now_ms - 120_000}]}
    sup.restart_stuck()
    assert restarted == ["p"]


def test_heartbeat_reports_platform_and_agents(sup):
    sup.heartbeat()
    hb = sup.transport.computer_updates[-1]
    assert hb["status"] == "online" and hb["platform"] in ("macos", "linux")
    assert hb["detected_agents"] == ["claude", "codex"]


def test_waiting_alert_fires_once_per_question(sup, monkeypatch):
    banners = []
    monkeypatch.setattr("aaw_core.host.supervisor.desktop_banner", lambda t, b: banners.append(t))
    sup.settings = Settings(state_dir=sup.settings.state_dir, relay_url="ws://x", computer_name="box",
                            keep_awake=False, waiting_alert_seconds=1)
    sup.cache = {"proj": {"pending_question_id": "q1", "agent": "claude"}}
    sup.check_waiting_alerts()
    assert banners == []
    sup.pending_since["proj"] -= 5
    sup.check_waiting_alerts()
    sup.check_waiting_alerts()
    assert banners == ["proj is waiting for your answer"]
    assert Path(sup.settings.state_dir).exists()


def test_refresh_cache_mirrors_the_documents_for_a_local_gui(sup):
    sup.transport.projects = {"proj": {"status": "running", "agent": "codex"}, "_supervisor": {}}
    sup.refresh_cache()
    assert sup.transport.mirrored == sup.transport.projects
    assert sup.cache["proj"]["agent"] == "codex" and "_supervisor" not in sup.cache


def test_stop_request_writes_stopped_right_away(sup, monkeypatch):
    monkeypatch.setattr(sessions, "session_alive", lambda p: True)
    monkeypatch.setattr(sessions, "stop_session", lambda settings, p: ["stopped it"])
    assert sup.handle_request("stop_session", {"project_id": "proj"}) == {"result": "stopped", "session_id": "proj"}
    pid, fields = sup.transport.project_writes[-1]
    assert pid == "proj" and fields["status"] == "stopped" and fields["pending_question_id"] == ""
