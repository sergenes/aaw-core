"""/restart keeps the phone's feed when the agent resumes its conversation."""

from __future__ import annotations

from aaw_core import daemon
from aaw_core.config import Settings


class FakeTransport:
    def __init__(self):
        self.calls: list[str] = []

    def clear_events(self):
        self.calls.append("clear_events")

    def init_local_log(self, is_reconnect=False):
        self.calls.append(f"init_local_log(reconnect={is_reconnect})")

    def set_project_status(self, status, **kw):
        self.calls.append(f"status={status}")

    def update_project(self, **fields):
        self.calls.append(f"update={sorted(fields)}")


def _restart(monkeypatch, tmp_path, resumed: bool) -> list[str]:
    monkeypatch.setattr(daemon.tmux, "resumes_conversation", lambda agent, d: resumed)
    monkeypatch.setattr(daemon.tmux, "restart_agent", lambda *a, **k: True)
    t = FakeTransport()
    settings = Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)
    cmd = {"payload": {"command": "text", "args": "/restart", "source": "desktop"}}
    daemon.handle_command(cmd, t, settings, session="aaw-p", agent="claude", project_dir=tmp_path, project_id="p")
    return t.calls


def test_restart_that_resumes_keeps_the_feed(monkeypatch, tmp_path):
    calls = _restart(monkeypatch, tmp_path, resumed=True)
    assert "clear_events" not in calls
    assert calls == ["status=running", "update=['background_agents']"]


def test_restart_that_starts_fresh_clears_the_feed(monkeypatch, tmp_path):
    calls = _restart(monkeypatch, tmp_path, resumed=False)
    assert calls == ["clear_events", "init_local_log(reconnect=False)",
                     "status=running", "update=['background_agents']"]


# ── stale commands: the age cap on deliver-on-reconnect ─────────────────────


def test_stale_notice_only_past_the_cap():
    now = 1_700_000_000.0
    fresh = {"ts": int((now - 60) * 1000), "payload": {"args": "hi"}}
    assert daemon.stale_notice(fresh, now_s=now) is None

    at_the_cap = {"ts": int((now - daemon.STALE_COMMAND_S) * 1000), "payload": {"args": "hi"}}
    assert daemon.stale_notice(at_the_cap, now_s=now) is None  # the cap itself is still fresh

    old = {"ts": int((now - daemon.STALE_COMMAND_S - 60) * 1000), "payload": {"args": "fix the tests"}}
    notice = daemon.stale_notice(old, now_s=now)
    assert notice is not None
    assert "fix the tests" in notice and "Not delivered" in notice and "31m" in notice


def test_stale_notice_ages_scheduled_prompts_from_deliver_at():
    now = 1_700_000_000.0
    # queued two days ago, due one minute ago: fresh, the 2 AM prompt must still fire
    due_now = {"ts": int((now - 2 * 86400) * 1000), "deliver_at": int((now - 60) * 1000),
               "payload": {"args": "run the nightly"}}
    assert daemon.stale_notice(due_now, now_s=now) is None

    # due three hours ago (the computer slept through it): expired, with the age in hours
    overslept = {"ts": int((now - 2 * 86400) * 1000), "deliver_at": int((now - 3 * 3600) * 1000),
                 "payload": {"args": "run the nightly"}}
    notice = daemon.stale_notice(overslept, now_s=now)
    assert notice is not None and "3h" in notice


def test_stale_notice_without_any_timestamp_is_fresh():
    assert daemon.stale_notice({"payload": {"args": "hi"}}, now_s=1_700_000_000.0) is None
