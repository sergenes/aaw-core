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
        self.notifications: list[tuple[str, str]] = []
        self.requests = None  # a queue.Queue when a test feeds phone requests
        self.responses: list[tuple[str, dict]] = []

    def update_computer(self, **fields):
        self.computer_updates.append(fields)

    def write_notification(self, message, level, set_running=False):
        self.notifications.append((message, level))

    def list_projects(self):
        return self.projects

    def list_commands(self, project_id):
        return self.commands.get(project_id, [])

    def set_project_fields(self, project_id, fields):
        self.project_writes.append((project_id, fields))

    def mirror_projects(self, docs):
        self.mirrored = docs


    def respond(self, request, payload):  # phone requests, fed by a test through `requests`
        self.responses.append((request.get("req"), payload))

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


def test_serve_requests_drains_the_queue_and_splits_parts(sup, monkeypatch):
    import queue as q
    import time

    monkeypatch.setattr(fs, "RAW_CHUNK", 4)
    (sup.home / "a.md").write_text("hello world!")  # 3 chunks
    sup.transport.requests = q.Queue()
    sup.transport.responses = []
    sup.transport.requests.put({"req": "r1", "kind": "fs_browse", "payload": {}})
    sup.transport.requests.put({"req": "r2", "kind": "fs_fetch",
                                "payload": {"path_enc": encrypt(str(sup.home / "a.md"), KEY), "parts": True}})
    sup.transport.requests.put({"req": "r3", "kind": "nope", "payload": {}})
    sup.serve_requests()  # no wait: answers all three and returns
    answered = [r for r, _ in sup.transport.responses]
    assert answered == ["r1", "r2", "r2", "r2", "r3"]
    assert [p.get("part") for r, p in sup.transport.responses if r == "r2"] == [0, 1, 2]
    assert sup.transport.responses[-1][1] == {"error": "unknown_request"}
    # an empty queue returns at once without waiting, and waits at most `wait` otherwise
    t0 = time.time()
    sup.serve_requests()
    sup.serve_requests(wait=0.2)
    assert 0.15 < time.time() - t0 < 1.0


def test_file_requests_do_not_wait_for_the_session_lock_but_session_requests_do(sup, monkeypatch):
    import queue as q
    import threading
    import time

    sup.transport.requests = q.Queue()
    sup.transport.responses = []
    monkeypatch.setattr(sup, "req_start_session", lambda payload: {"result": "started"})
    with sup.session_lock:  # a watchdog duty is running
        sup.transport.requests.put({"req": "browse", "kind": "fs_browse", "payload": {}})
        sup.serve_requests()
        assert [r for r, _ in sup.transport.responses] == ["browse"]
        sup.transport.requests.put({"req": "start", "kind": "start_session", "payload": {"project_id": "p"}})
        worker = threading.Thread(target=sup.serve_requests, daemon=True)
        worker.start()
        time.sleep(0.3)
        assert [r for r, _ in sup.transport.responses] == ["browse"]  # still waiting for the duty
    worker.join(timeout=2)
    assert [r for r, _ in sup.transport.responses] == ["browse", "start"]


def test_fs_fetch_in_parts_splits_a_chunked_reply_into_one_frame_per_chunk(sup, monkeypatch):
    from aaw_core.host import supervisor as sv

    monkeypatch.setattr(fs, "RAW_CHUNK", 4)
    (sup.home / "big.png").write_bytes(b"0123456789ab")  # 3 chunks of 4
    payload = {"path_enc": encrypt(str(sup.home / "big.png"), KEY), "parts": True}
    reply = sup.handle_request("fs_fetch", payload)
    assert reply["total_chunks"] == 3 and reply["mime"] == "image/png"
    parts = sv.Supervisor.response_parts(reply)
    assert [p["part"] for p in parts] == [0, 1, 2]
    assert all(p["total_chunks"] == 3 and p["size"] == 12 and p["error"] == "" and len(p["chunks"]) == 1 for p in parts)
    joined = b"".join(base64.b64decode(decrypt(p["chunks"][0], KEY)) for p in parts)
    assert joined == b"0123456789ab"
    # one chunk, or an error: a single frame, unchanged
    assert sv.Supervisor.response_parts({"error": "too_large", "total_chunks": 0}) == [{"error": "too_large", "total_chunks": 0}]
    small = sup.handle_request("fs_fetch", {"path_enc": encrypt(str(sup.home / "big.png"), KEY)})
    monkeypatch.setattr(fs, "RAW_CHUNK", 1 << 20)
    one = sup.handle_request("fs_fetch", {"path_enc": encrypt(str(sup.home / "big.png"), KEY), "parts": True})
    assert sv.Supervisor.response_parts(one) == [one]
    # without parts the single-frame cap holds; with parts the larger one
    monkeypatch.setattr(fs, "MAX_FETCH_BYTES", 4)
    assert sup.handle_request("fs_fetch", {"path_enc": encrypt(str(sup.home / "big.png"), KEY)})["error"] == fs.ERR_TOO_LARGE
    assert sup.handle_request("fs_fetch", payload)["error"] == ""
    assert small["total_chunks"] == 3


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


def _drive_pressure(sup, levels):
    """Feed the monitor a fixed sequence of samples on a controllable clock."""
    from aaw_core.host import mempressure
    seq = iter(levels)
    clock = [0.0]
    sup.pressure = mempressure.PressureMonitor(sampler=lambda: next(seq), now=lambda: clock[0])
    return clock


def test_memory_pressure_warns_once_badges_and_clears(sup):
    # two critical samples (SUSTAIN=2) warn once and badge; a later normal clears.
    clock = _drive_pressure(sup, ["critical", "critical", "critical", "normal"])
    sup.check_memory_pressure()
    assert sup.memory_critical is True
    assert sup.transport.notifications == []  # one critical sample: not sustained yet
    sup.check_memory_pressure()
    assert len(sup.transport.notifications) == 1
    msg, level = sup.transport.notifications[0]
    assert "box" in msg and "memory" in msg and level == "warning"
    assert {"memory_pressure": "critical"} in sup.transport.computer_updates
    sup.check_memory_pressure()  # still critical, inside REPEAT_EVERY: no second push
    assert len(sup.transport.notifications) == 1
    clock[0] += 10
    sup.check_memory_pressure()  # normal now: badge cleared, flag down
    assert sup.memory_critical is False
    assert {"memory_pressure": ""} in sup.transport.computer_updates


def test_restart_stuck_stands_down_under_memory_pressure(sup, monkeypatch):
    import time
    monkeypatch.setattr(sessions, "list_sessions", lambda: [sessions.Session("aaw-p", "p", "/tmp", False)])
    restarted = []
    monkeypatch.setattr(sessions, "start_daemon", lambda s, d, pid, agent: restarted.append(pid) or [])
    now_ms = int(time.time() * 1000)
    sup.transport.commands = {"p": [{"type": "command", "ts": now_ms - 120_000}]}
    sup.memory_critical = True
    sup.restart_stuck()
    assert restarted == []  # an old command under pressure is thrashing, not wedged
    sup.memory_critical = False
    sup.restart_stuck()
    assert restarted == ["p"]  # once pressure clears, the real stuck check runs


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
