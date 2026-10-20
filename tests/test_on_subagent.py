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


def _record(tmp_path, session: str, agent_id: str, finished) -> str:
    """Claude's layout: <projects>/<session>.jsonl next to <session>/subagents/agent-<id>.meta.json."""
    transcript = tmp_path / "claude" / f"{session}.jsonl"
    d = tmp_path / "claude" / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    if finished is not None:
        (d / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": "general-purpose",
                                                                   "finished": finished}))
    return str(transcript)


def test_reconcile_drops_only_agents_claude_recorded_as_finished(tmp_path):
    # Seen 2026-10-18: a background agent ended on "out of usage credits", Claude fired no
    # SubagentStop, and the phone showed "Waiting for 1 background agent" for hours.
    s = _settings(tmp_path)
    t = _record(tmp_path, "sess", "dead", True)
    _record(tmp_path, "sess", "alive", False)
    for agent_id in ("dead", "alive", "unknown"):
        on_subagent.update_subagents(s, "p", add=agent_id)
    assert on_subagent.reconcile_subagents(s, "p", t) == ["alive", "unknown"]
    assert on_subagent.update_subagents(s, "p") == ["alive", "unknown"]
    assert on_subagent.reconcile_subagents(s, "p", t) is None       # nothing more to drop
    assert on_subagent.reconcile_subagents(s, "p", "") is None      # no transcript: no guess
    assert on_subagent.reconcile_subagents(s, "p", str(tmp_path / "nowhere.jsonl")) is None


def test_reconcile_uses_the_remembered_transcript(tmp_path):
    s = _settings(tmp_path)
    on_subagent.update_subagents(s, "p", add="dead")
    assert on_subagent.reconcile_subagents(s, "p") is None          # no sidecar yet
    t = _record(tmp_path, "sess", "dead", True)
    on_subagent.remember_transcript(s, "p", t)
    assert on_subagent.transcript_file(s, "p").read_text() == t
    assert on_subagent.reconcile_subagents(s, "p") == []


def test_a_resumed_agent_is_one_entry_and_a_dead_one_clears_on_the_next_event(monkeypatch, tmp_path):
    # Claude fires SubagentStart again, same id, when the coordinator resumes an agent.
    s = _settings(tmp_path)
    t = _record(tmp_path, "sess", "a1", None)
    tr = _Transport()
    start = {"hook_event_name": "SubagentStart", "agent_id": "a1", "transcript_path": t}
    assert _run(monkeypatch, tmp_path, start, tr) == 0
    assert _run(monkeypatch, tmp_path, start, tr) == 0
    assert tr.projects[-1] == {"background_agents": ["a1"]}
    # a1 dies on a usage limit (record says finished, no Stop); a2 starts later.
    _record(tmp_path, "sess", "a1", True)
    assert _run(monkeypatch, tmp_path, {"hook_event_name": "SubagentStart", "agent_id": "a2",
                                        "transcript_path": t}, tr) == 0
    assert tr.projects[-1] == {"background_agents": ["a2"]}
    assert on_subagent.transcript_file(s, "p").read_text() == t


def test_malformed_records_are_ignored(tmp_path):
    s = _settings(tmp_path)
    t = _record(tmp_path, "sess", "a1", None)
    d = on_subagent.agents_dir(t)
    assert d is not None
    (d / "agent-a1.meta.json").write_text("{not json")
    (d / "agent-a2.meta.json").write_text(json.dumps({"finished": "yes"}))  # not the boolean
    on_subagent.update_subagents(s, "p", add="a1")
    on_subagent.update_subagents(s, "p", add="a2")
    assert on_subagent.finished_ids(["a1", "a2"], t) == []
    assert on_subagent.reconcile_subagents(s, "p", t) is None
