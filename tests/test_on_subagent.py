"""The running-subagents set behind "Waiting for N background agents to finish"."""

from __future__ import annotations

import io
import json

from aaw_core.config import Settings
from aaw_core.hooks import on_subagent


def _settings(tmp_path) -> Settings:
    return Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)


def test_update_subagents_add_discard_clear(tmp_path):
    s = _settings(tmp_path)
    assert on_subagent.update_subagents(s, "p", add="a1") == ["a1"]
    assert on_subagent.update_subagents(s, "p", add="a2") == ["a1", "a2"]
    assert on_subagent.update_subagents(s, "p", discard="a1") == ["a2"]
    assert on_subagent.update_subagents(s, "p", discard="never-seen") == ["a2"]
    assert on_subagent.update_subagents(s, "p", clear=True) == []
    on_subagent.subagents_file(s, "p").write_text("not json")
    assert on_subagent.update_subagents(s, "p", add="a3") == ["a3"]  # a corrupt file heals


class _Transport:
    def __init__(self):
        self.projects: list[dict] = []
        self.stopped = False

    def update_project(self, **fields):
        self.projects.append(fields)

    def stop(self):
        self.stopped = True


def _run(monkeypatch, tmp_path, payload: dict, transport, *, gone: bool = False) -> int:
    s = _settings(tmp_path)
    monkeypatch.setattr(on_subagent, "preamble", lambda: (s, "p", "claude"))
    monkeypatch.setattr(on_subagent, "read_payload", lambda: payload)
    monkeypatch.setattr(on_subagent, "open_transport", lambda *a, **k: transport)
    monkeypatch.setattr(on_subagent, "session_confirmed_gone", lambda session: gone)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return on_subagent.main()


def test_start_and_stop_mirror_the_set_to_the_project(monkeypatch, tmp_path):
    t = _Transport()
    assert _run(monkeypatch, tmp_path, {"hook_event_name": "SubagentStart", "agent_id": "a1"}, t) == 0
    assert _run(monkeypatch, tmp_path, {"hook_event_name": "SubagentStart", "agent_id": "a2"}, t) == 0
    assert _run(monkeypatch, tmp_path, {"hook_event_name": "SubagentStop", "agent_id": "a1"}, t) == 0
    assert t.projects == [{"background_agents": ["a1"]},
                          {"background_agents": ["a1", "a2"]},
                          {"background_agents": ["a2"]}]
    assert t.stopped


def test_a_late_start_after_the_session_died_is_dropped(monkeypatch, tmp_path):
    # A SubagentStart can arrive after the tmux session was killed (the hook process
    # outlives the pane briefly). It must not resurrect state for a dead session.
    t = _Transport()
    assert _run(monkeypatch, tmp_path, {"hook_event_name": "SubagentStart", "agent_id": "a1"}, t,
                gone=True) == 0
    assert t.projects == []
    s = _settings(tmp_path)
    assert not on_subagent.subagents_file(s, "p").exists()


def test_an_internal_helpers_stop_is_silent(monkeypatch, tmp_path):
    t = _Transport()
    # Claude fires SubagentStop for internal helper agents that never had a Start;
    # confirmed live on 2.1.285. Nothing is written, nothing reaches the relay.
    assert _run(monkeypatch, tmp_path, {"hook_event_name": "SubagentStop", "agent_id": "helper"}, t) == 0
    assert t.projects == []
    s = _settings(tmp_path)
    assert json.loads(on_subagent.subagents_file(s, "p").read_text() or "[]") == []


def test_session_confirmed_gone_needs_positive_confirmation(monkeypatch):
    from aaw_core.host import tmux

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def listing(result):
        monkeypatch.setattr(tmux, "tmux_run", lambda *a, **k: result)

    listing(_R(0, out="aaw-p\naaw-q\n"))
    assert not tmux.session_confirmed_gone("aaw-p")        # listed: alive
    listing(_R(0, out="aaw-q\n"))
    assert tmux.session_confirmed_gone("aaw-p")            # server answered without it
    listing(_R(1, err="no server running on /tmp/tmux-501/default"))
    assert tmux.session_confirmed_gone("aaw-p")            # no server at all
    listing(_R(1, err="lost server"))
    assert not tmux.session_confirmed_gone("aaw-p")        # ambiguous error: not gone

    def boom(*a, **k):
        raise OSError("tmux missing")
    monkeypatch.setattr(tmux, "tmux_run", boom)
    assert not tmux.session_confirmed_gone("aaw-p")        # cannot tell: not gone
