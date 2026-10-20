import json

from aaw_core.encryption import encrypt, generate_key_b64
from aaw_core.host import state
from aaw_core.transport.base import decrypt_field

KEY = generate_key_b64()


def test_merge_decrypts_known_fields_and_is_0600(tmp_path):
    d = tmp_path / "projects"
    doc = state.merge_project(d, "proj", {"status": "running", "project_path": encrypt("/home/me/proj", KEY)},
                              lambda v: decrypt_field(v, KEY))
    assert doc == {"project_id": "proj", "status": "running", "project_path": "/home/me/proj"}
    path = d / "proj.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    state.merge_project(d, "proj", {"pending_question_id": "q1"})
    assert json.loads(path.read_text())["pending_question_id"] == "q1"
    assert json.loads(path.read_text())["status"] == "running"  # merged, not replaced


def test_replace_removes_stale_documents_and_bad_key_yields_empty(tmp_path):
    d = tmp_path / "projects"
    state.merge_project(d, "old", {"status": "stopped"})
    other = generate_key_b64()
    state.replace_projects(d, {"new": {"last_event_summary": encrypt("secret", other)}},
                           lambda v: decrypt_field(v, KEY))
    docs = state.read_projects(d)
    assert set(docs) == {"new"}
    assert docs["new"]["last_event_summary"] == ""  # a re-paired key: never ciphertext, never a crash


def test_read_is_empty_when_nothing_mirrored(tmp_path):
    assert state.read_projects(tmp_path / "missing") == {}
    assert state.read_project(tmp_path / "missing", "x") == {}


def test_transport_mirrors_the_scheduled_list(tmp_path):
    from aaw_core.transport.base import make_command
    from aaw_core.transport.relay import RelayTransport
    t = RelayTransport(relay_url="ws://unused", token="t", computer_id="c", project_id="proj",
                       sessions_dir=tmp_path / "sessions", enc_key=KEY)
    later = 4_102_444_800_000  # far future
    doc = make_command("tonight's prompt", KEY, deliver_at=later)
    t._inbox[doc["id"]] = doc
    assert t.poll_commands() == []
    mirrored = state.read_project(tmp_path / "projects", "proj")
    assert mirrored["scheduled_prompts"] == [{"id": doc["id"], "deliver_at": later, "text": "tonight's prompt"}]
    assert mirrored["scheduled_count"] == 1


def test_transport_mirrors_pushable_alerts(tmp_path):
    from aaw_core.transport.relay import RelayTransport
    t = RelayTransport(relay_url="ws://unused", token="t", computer_id="c", project_id="proj",
                       sessions_dir=tmp_path / "sessions", enc_key=KEY)
    t.write_event("notification", {"message": "Claude is waiting for your input", "level": "info"})
    assert "last_alert" not in state.read_project(tmp_path / "projects", "proj")
    eid = t.write_event("notification", {"message": "Response ready: done", "level": "success"})
    alert = state.read_project(tmp_path / "projects", "proj")["last_alert"]
    assert alert["id"] == eid and alert["level"] == "success" and alert["message"] == "Response ready: done"


def test_activity_time_moves_with_events_not_with_status_changes(tmp_path):
    # Seen on the phone 2026-10-20: three untouched sessions sat above the one in use,
    # because every daemon restart and idle check had stamped them as just touched.
    from aaw_core.transport.relay import RelayTransport
    t = RelayTransport(relay_url="ws://unused", token="t", computer_id="c", project_id="proj",
                       sessions_dir=tmp_path / "sessions", enc_key=KEY)
    sent = []
    t._send = sent.append
    def stamps():
        return [f["fields"].get("last_event_ts") for f in sent if f["type"] == "state"]
    t.set_project_status("running", pending_question_id="")      # a restart
    t.set_project_status("idle", pending_question_id="")         # the idle check
    assert stamps() == [None, None]
    sent.clear()
    t.set_project_status("running", last_event_summary="Ran: pytest")  # a tool call
    assert stamps() == [stamps()[0]] and stamps()[0] is not None
    sent.clear()
    t.write_event("message", {"role": "user", "content": "hi"})
    t.write_event("notification", {"message": "waiting", "level": "info"})
    assert [f["type"] for f in sent] == ["event", "state", "event"]  # the message stamps, the notice does not
    assert sent[1]["fields"]["last_event_ts"] is not None
