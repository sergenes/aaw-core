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
