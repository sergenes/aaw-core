"""A session deleted from the phone stops for real on the computer."""

from __future__ import annotations

from aaw_core.host import state
from aaw_core.transport.relay import RelayTransport


def _transport(tmp_path) -> RelayTransport:
    return RelayTransport(relay_url="wss://example/v1/ws", token="t", computer_id="c",
                          project_id="p", sessions_dir=tmp_path / "sessions")


def test_a_forwarded_project_delete_sets_the_flag(tmp_path):
    t = _transport(tmp_path)
    assert not t.delete_requested.is_set()
    t._on_frame({"type": "project_delete", "project_id": "other"})
    assert not t.delete_requested.is_set()  # another session's delete is not ours
    t._on_frame({"type": "project_delete", "project_id": "p"})
    assert t.delete_requested.is_set()


def test_delete_local_mirror_forgets_the_doc(tmp_path):
    t = _transport(tmp_path)
    d = state.mirror_dir((tmp_path / "sessions").parent)
    state.merge_project(d, "p", {"status": "running"})
    state.merge_project(d, "q", {"status": "idle"})
    t.delete_local_mirror()
    assert state.read_projects(d).keys() == {"q"}
    t.delete_local_mirror()  # already gone: a no-op
