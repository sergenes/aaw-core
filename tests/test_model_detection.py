"""Which model a session runs, for the phone's "Codex · GPT-6 Astra" label: the hook input
first, the agent's transcript as the fallback, never a guess from config defaults.

The Codex fixtures are synthetic, shaped like a real 0.164 rollout (record types and the
fields read here); no real conversation is stored."""

from __future__ import annotations

import json

import pytest

from aaw_core.config import Settings
from aaw_core.hooks import on_session_start, on_stop_claude, on_stop_cursor, on_stop_grok, on_user_prompt
from aaw_core.host import hooks_installer as hi


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "home", lambda: tmp_path)
    return tmp_path


def _codex_turn(model: str, filler_items: int) -> list[str]:
    """One Codex turn: the turn's model records first, then its work."""
    lines = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}},
        {"type": "turn_context", "payload": {"turn_id": "t", "cwd": "/w", "model": model, "effort": "medium"}},
        {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_settings": {"model": model}}},
    ]
    for i in range(filler_items):  # tool calls and their output, no model field in them
        lines.append({"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "id": f"exec-{i}", "aggregated_output": "x" * 900}}})
        lines.append({"type": "token_usage_record", "payload": {"input_tokens": i}})
    return [json.dumps(entry) for entry in lines]


def _write(path, lines, tail: str = "") -> str:
    path.write_text("\n".join(lines) + "\n" + tail)
    return str(path)


def test_a_long_codex_turn_names_its_model_megabytes_from_the_end(tmp_path):
    """2026-10-23: one Codex turn grew its rollout to 2 MB; its model was recorded only at
    the turn's start, beyond the 256 KB tail the parser read, so the phone showed none."""
    meta = json.dumps({"type": "session_meta", "payload": {"id": "s", "cli_version": "0.164.0"}})
    path = _write(tmp_path / "rollout.jsonl", [meta] + _codex_turn("gpt-6-astra", 1500))
    assert (tmp_path / "rollout.jsonl").stat().st_size > 1_000_000
    assert on_stop_claude.model_from_transcript(path, "codex") == "gpt-6-astra"


def test_the_newest_turn_wins_after_a_model_switch(tmp_path):
    path = _write(tmp_path / "r.jsonl", _codex_turn("gpt-6-astra", 50) + _codex_turn("gpt-6-sol", 50))
    assert on_stop_claude.model_from_transcript(path, "codex") == "gpt-6-sol"


def test_lines_split_across_scan_blocks_stay_whole(tmp_path, monkeypatch):
    """The backward scan reads fixed blocks; a record cut by a block edge is joined with
    its beginning from the earlier block, at every edge position."""
    lines = _codex_turn("gpt-6-astra", 3)
    path = _write(tmp_path / "r.jsonl", lines)
    size = (tmp_path / "r.jsonl").stat().st_size
    for block in range(7, size + 1, 13):
        monkeypatch.setattr(on_stop_claude, "MODEL_BLOCK_BYTES", block)
        assert on_stop_claude.model_from_transcript(path, "codex") == "gpt-6-astra", block


def test_a_line_still_being_written_is_skipped(tmp_path):
    cut = json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-sol"}})[:30]
    path = _write(tmp_path / "r.jsonl", _codex_turn("gpt-6-astra", 2), tail=cut)
    assert on_stop_claude.model_from_transcript(path, "codex") == "gpt-6-astra"


def test_the_scan_window_bounds_the_search(tmp_path, monkeypatch):
    path = _write(tmp_path / "r.jsonl", _codex_turn("gpt-6-astra", 200))
    monkeypatch.setattr(on_stop_claude, "MODEL_SCAN_BYTES", 4096)
    assert on_stop_claude.model_from_transcript(path, "codex") == ""  # too far back: unknown


def test_claude_transcripts_and_placeholders(tmp_path):
    entries = [{"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5"}},
               {"type": "user", "message": {"role": "user", "model": "not-this"}},
               {"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>"}}]
    path = _write(tmp_path / "c.jsonl", [json.dumps(e) for e in entries])
    assert on_stop_claude.model_from_transcript(path) == "claude-opus-5-5"


def test_the_hook_input_wins_and_the_transcript_is_the_fallback(tmp_path):
    path = _write(tmp_path / "r.jsonl", _codex_turn("gpt-6-astra", 5))
    assert on_stop_claude.active_model({"model": "gpt-6-sol", "transcript_path": path}, "codex") == "gpt-6-sol"
    assert on_stop_claude.active_model({"transcript_path": path}, "codex") == "gpt-6-astra"  # older Codex
    assert on_stop_claude.active_model({"model": "", "transcript_path": path}, "codex") == "gpt-6-astra"
    assert on_stop_claude.active_model({"model": {"id": "x"}}, "codex") == ""  # not a slug
    assert on_stop_claude.active_model({"model": "auto"}, "codex") == ""
    assert on_stop_claude.active_model({}, "codex") == ""  # unknown, never a guess


class _Transport:
    def __init__(self):
        self.updates: list[dict] = []
        self.events: list[tuple] = []

    def update_project(self, **fields):
        self.updates.append(fields)

    def write_event(self, kind, payload, **kw):
        self.events.append((kind, payload))

    def clear_events(self):
        pass

    def stop(self, *a, **kw):
        pass


def _run(module, monkeypatch, settings, agent, payload) -> _Transport:
    transport = _Transport()
    monkeypatch.setattr(module, "preamble", lambda: (settings, "p", agent))
    monkeypatch.setattr(module, "read_payload", lambda: payload)
    monkeypatch.setattr(module, "open_transport", lambda s, p: transport)
    assert module.main() == 0
    return transport


def test_a_codex_prompt_stamps_the_model_as_the_turn_starts(settings, monkeypatch, tmp_path):
    stale = _write(tmp_path / "r.jsonl", _codex_turn("gpt-6-astra", 2))
    t = _run(on_user_prompt, monkeypatch, settings, "codex",
             {"prompt": "fix it", "model": "gpt-6-sol", "transcript_path": stale})
    assert t.updates == [{"model": "gpt-6-sol"}]
    assert t.events == [("message", {"role": "user", "content": "fix it", "agent": "codex"})]
    # a slash command is not a feed row, but the model still updates
    assert _run(on_user_prompt, monkeypatch, settings, "codex",
                {"prompt": "/status", "model": "gpt-6-sol"}).updates == [{"model": "gpt-6-sol"}]
    # without the field the transcript is not read here: its turn record may not exist yet
    assert _run(on_user_prompt, monkeypatch, settings, "codex",
                {"prompt": "go", "transcript_path": stale}).updates == []
    assert _run(on_user_prompt, monkeypatch, settings, "gemini",
                {"prompt": "go", "model": "gemini-3"}).updates == []


def test_a_new_session_is_stamped_from_the_hook_input_else_left_unknown(settings, monkeypatch, tmp_path):
    fresh = str(tmp_path / "fresh.jsonl")
    assert _run(on_session_start, monkeypatch, settings, "codex",
                {"source": "startup", "model": "gpt-6-astra", "transcript_path": fresh}).updates == [{"model": "gpt-6-astra"}]
    assert _run(on_session_start, monkeypatch, settings, "claude",
                {"source": "startup", "model": "claude-fable-5-1"}).updates == [{"model": "claude-fable-5-1"}]
    assert _run(on_session_start, monkeypatch, settings, "codex",
                {"source": "startup", "transcript_path": fresh}).updates == [{"model": ""}]
    resumed = _write(tmp_path / "old.jsonl", _codex_turn("gpt-6-sol", 3))
    assert _run(on_session_start, monkeypatch, settings, "codex",
                {"source": "resume", "transcript_path": resumed}).updates == [{"model": "gpt-6-sol"}]
    assert _run(on_session_start, monkeypatch, settings, "codex",
                {"source": "fork", "model": "gpt-6-astra"}).updates == [{"model": "gpt-6-astra"}]


def test_gemini_names_the_model_in_each_answer_of_its_transcript(tmp_path):
    """Gemini CLI 0.71: the hook input names no model, and the footer can say "Auto"; each
    answer in the session's JSONL names the model that wrote it. Shaped like a real file."""
    lines = [
        {"sessionId": "s", "projectHash": "h", "kind": "main"},
        {"id": "1", "type": "user", "content": [{"text": 'the "model" is not named here'}]},
        {"$set": {"lastUpdated": "2026-11-27T04:56:01.808Z"}},
        {"id": "2", "type": "gemini", "content": "", "thoughts": [], "tokens": {"total": 1}},  # still thinking
        {"id": "3", "type": "gemini", "content": "PONG", "tokens": {"total": 2}, "model": "gemini-3.8-flash"},
        {"id": "4", "type": "user", "content": [{"text": "again"}], "model": "not-this"},
        {"$set": {"lastUpdated": "2026-11-27T04:56:19.252Z"}},
    ]
    path = _write(tmp_path / "session.jsonl", [json.dumps(e) for e in lines])
    assert on_stop_claude.active_model({"transcript_path": path, "hook_event_name": "AfterAgent"}, "gemini") == "gemini-3.8-flash"
    switched = lines + [{"id": "5", "type": "gemini", "content": "ok", "model": "gemini-3.8-pro"}]
    path = _write(tmp_path / "session.jsonl", [json.dumps(e) for e in switched])
    assert on_stop_claude.active_model({"transcript_path": path}, "gemini") == "gemini-3.8-pro"
    assert on_stop_claude.active_model({"transcript_path": str(tmp_path / "none.jsonl")}, "gemini") == ""


def _grok_session(home, monkeypatch, summary: dict | None) -> dict:
    """A Grok session folder under a fake home, and the hook input that names it."""
    monkeypatch.setattr(on_stop_grok.Path, "home", lambda: home)
    for name in ("GROK_SESSION_ID", "GROK_WORKSPACE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    folder = home / ".grok" / "sessions" / "%2Fw%2Fmy%20proj" / "sid"
    folder.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        (folder / "summary.json").write_text(json.dumps(summary))
    return {"sessionId": "sid", "workspaceRoot": "/w/my proj/", "cwd": "/w/my proj"}


def test_grok_names_the_model_in_its_session_summary(tmp_path, monkeypatch):
    """Grok 1.0.92: no model in the hook input; summary.json holds `current_model_id`."""
    payload = _grok_session(tmp_path, monkeypatch, {"current_model_id": "grok-4.7", "reasoning_effort": "high"})
    assert on_stop_claude.active_model(payload, "grok") == "grok-4.7"
    assert on_stop_claude.active_model({"sessionId": "other", "cwd": "/w/my proj"}, "grok") == ""  # no such session
    assert on_stop_claude.active_model({}, "grok") == ""
    monkeypatch.setenv("GROK_SESSION_ID", "sid")  # the hook's environment names the session too
    monkeypatch.setenv("GROK_WORKSPACE_ROOT", "/w/my proj")
    assert on_stop_claude.active_model({}, "grok") == "grok-4.7"


def test_a_grok_summary_without_a_model_is_unknown(tmp_path, monkeypatch):
    payload = _grok_session(tmp_path, monkeypatch, None)
    assert on_stop_grok.session_model(payload) == ""  # a session that has not answered yet
    for summary in ('{"current_model_id": null}', '{"current_model_id": 7}', "[]", "{not json"):
        (tmp_path / ".grok" / "sessions" / "%2Fw%2Fmy%20proj" / "sid" / "summary.json").write_text(summary)
        assert on_stop_grok.session_model(payload) == "", summary


def test_grok_is_stamped_at_start_and_at_each_prompt(settings, tmp_path, monkeypatch):
    payload = _grok_session(tmp_path, monkeypatch, None)
    # a fresh session has no summary yet: the label an earlier session left is cleared
    assert _run(on_session_start, monkeypatch, settings, "grok", {**payload, "source": "new"}).updates == [{"model": ""}]
    _grok_session(tmp_path, monkeypatch, {"current_model_id": "grok-4.6"})
    assert _run(on_session_start, monkeypatch, settings, "grok",
                {**payload, "source": "resume"}).updates == [{"model": "grok-4.6"}]
    t = _run(on_user_prompt, monkeypatch, settings, "grok", {**payload, "prompt": "<user_query>go</user_query>"})
    assert t.updates == [{"model": "grok-4.6"}]
    assert t.events == [("message", {"role": "user", "content": "go", "agent": "grok"})]


def _cursor_store(home, session_id: str, meta, *, as_hex: bool = True):
    """A Cursor session store shaped like 2026.11.19's: one `meta` row, JSON as hex text."""
    import sqlite3
    folder = home / ".cursor" / "chats" / "3fa251e03e529ca2721a2a3e20383a16" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "store.db").unlink(missing_ok=True)
    conn = sqlite3.connect(folder / "store.db")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    text = meta if isinstance(meta, str) else json.dumps(meta)
    conn.execute("INSERT INTO meta VALUES ('0', ?)", (text.encode().hex() if as_hex else text,))
    conn.commit()
    conn.close()


def test_cursor_names_the_model_in_its_session_store(tmp_path, monkeypatch):
    """Cursor's transcript names no model and its hooks can stay silent; the session's own
    store records `lastUsedModel`, "default" when Cursor picks (its screen says "Auto")."""
    monkeypatch.setattr(on_stop_cursor.Path, "home", lambda: tmp_path)
    _cursor_store(tmp_path, "sid-1", {"agentId": "sid-1", "mode": "default", "lastUsedModel": "grok-4.6"})
    assert on_stop_cursor.session_model("sid-1") == "grok-4.6"
    _cursor_store(tmp_path, "sid-1", {"agentId": "sid-1", "lastUsedModel": "default"})
    assert on_stop_cursor.session_model("sid-1") == "Auto"
    _cursor_store(tmp_path, "sid-1", {"lastUsedModel": "gpt-6-astra"}, as_hex=False)  # plain JSON reads too
    assert on_stop_cursor.session_model("sid-1") == "gpt-6-astra"
    for meta in ({"agentId": "sid-1"}, {"lastUsedModel": None}, {"lastUsedModel": {"id": "x"}}, [], "zz not json"):
        _cursor_store(tmp_path, "sid-1", meta)
        assert on_stop_cursor.session_model("sid-1") == "", meta
    assert on_stop_cursor.session_model("no-such-session") == ""
    assert on_stop_cursor.session_model("") == ""
    assert on_stop_cursor.session_model("../sid-1") == ""  # never a path


def test_a_cursor_store_that_is_not_a_database_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(on_stop_cursor.Path, "home", lambda: tmp_path)
    folder = tmp_path / ".cursor" / "chats" / "h" / "sid-2"
    folder.mkdir(parents=True)
    (folder / "store.db").write_text("not sqlite")
    assert on_stop_cursor.session_model("sid-2") == ""


def test_a_cursor_hook_finds_its_session_by_the_transcript_name(tmp_path, monkeypatch):
    monkeypatch.setattr(on_stop_cursor.Path, "home", lambda: tmp_path)
    _cursor_store(tmp_path, "sid-3", {"lastUsedModel": "default"})
    transcript = "/x/.cursor/projects/w/agent-transcripts/sid-3/sid-3.jsonl"
    assert on_stop_cursor.session_id_of(transcript) == "sid-3"
    assert on_stop_claude.active_model({"transcript_path": transcript}, "cursor") == "Auto"
    assert on_stop_claude.active_model({"conversation_id": "sid-3"}, "cursor") == "Auto"
    # no store for the session: what the hook input names, in the same words
    assert on_stop_claude.active_model({"conversation_id": "gone", "model": "default"}, "cursor") == "Auto"
    assert on_stop_claude.active_model({"conversation_id": "gone", "model": "gpt-6-sol"}, "cursor") == "gpt-6-sol"
    assert on_stop_claude.active_model({}, "cursor") == ""


def test_codex_hook_entries_keep_their_trust(home):
    """Codex trusts each hook by the hash of its definition: a new hook or a changed one is
    skipped, with a startup warning, until the user reviews it in /hooks. Reinstalling must
    leave every entry exactly as it was, and add no SessionStart."""
    hi.update_codex_hooks()
    first = (home / ".codex" / "hooks.json").read_text()
    hi.update_codex_hooks()
    data = json.loads((home / ".codex" / "hooks.json").read_text())
    assert (home / ".codex" / "hooks.json").read_text() == first
    assert sorted(data["hooks"]) == ["PostToolUse", "PreToolUse", "Stop", "UserPromptSubmit"]
