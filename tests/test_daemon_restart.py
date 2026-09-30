"""/restart keeps the phone's feed when the agent resumes its conversation."""

from __future__ import annotations

from aaw_core import daemon


class FakeTransport:
    def __init__(self):
        self.calls: list[str] = []

    def clear_events(self):
        self.calls.append("clear_events")

    def init_local_log(self, is_reconnect=False):
        self.calls.append(f"init_local_log(reconnect={is_reconnect})")

    def set_project_status(self, status, **kw):
        self.calls.append(f"status={status}")


def _restart(monkeypatch, tmp_path, resumed: bool) -> list[str]:
    monkeypatch.setattr(daemon.tmux, "resumes_conversation", lambda agent, d: resumed)
    monkeypatch.setattr(daemon.tmux, "restart_agent", lambda *a, **k: True)
    t = FakeTransport()
    cmd = {"payload": {"command": "text", "args": "/restart", "source": "desktop"}}
    daemon.handle_command(cmd, t, None, session="aaw-p", agent="claude", project_dir=tmp_path, project_id="p")
    return t.calls


def test_restart_that_resumes_keeps_the_feed(monkeypatch, tmp_path):
    calls = _restart(monkeypatch, tmp_path, resumed=True)
    assert "clear_events" not in calls
    assert calls == ["status=running"]


def test_restart_that_starts_fresh_clears_the_feed(monkeypatch, tmp_path):
    calls = _restart(monkeypatch, tmp_path, resumed=False)
    assert calls == ["clear_events", "init_local_log(reconnect=False)", "status=running"]
