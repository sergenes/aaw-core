"""The hooks' pure logic: per-agent output dialects, payload normalization, question
text, transcript parsing, the Grok pane extractor, the reset-time parser, the
stray-dump guard, the status-message table, and the shared helpers."""

from __future__ import annotations

import io
import json
import time

import pytest

from aaw_core.config import Settings
from aaw_core.hooks import (
    common,
    on_notification,
    on_post_tool,
    on_pre_tool,
    on_session_start,
    on_stop,
    on_stop_claude,
    on_stop_cursor,
    on_stop_gemini,
    on_stop_grok,
    on_user_prompt,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)


# ── common ──────────────────────────────────────────────────────────────────


def test_env_precedence_and_defaults(monkeypatch):
    monkeypatch.delenv("AAW_PROJECT", raising=False)
    monkeypatch.delenv("AGENT_BRIDGE_PROJECT", raising=False)
    monkeypatch.delenv("AAW_AGENT", raising=False)
    monkeypatch.delenv("AGENT_BRIDGE_AGENT", raising=False)
    assert common.env_project() == "" and common.env_agent() == "claude"
    monkeypatch.setenv("AGENT_BRIDGE_PROJECT", "legacy")
    monkeypatch.setenv("AGENT_BRIDGE_AGENT", "codex")
    assert common.env_project() == "legacy" and common.env_agent() == "codex"
    monkeypatch.setenv("AAW_PROJECT", "new")
    monkeypatch.setenv("AAW_AGENT", "gemini")
    assert common.env_project() == "new" and common.env_agent() == "gemini"  # the new names win


def test_preamble_exits_unless_enabled_and_bridged(monkeypatch, settings):
    monkeypatch.setenv("AAW_STATE_DIR", str(settings.state_dir))
    monkeypatch.setenv("AAW_PROJECT", "proj")
    with pytest.raises(SystemExit) as e:
        common.preamble()  # not enabled
    assert e.value.code == 0
    settings.enabled_flag.parent.mkdir(parents=True, exist_ok=True)
    settings.enabled_flag.write_text("")
    monkeypatch.delenv("AAW_PROJECT")
    monkeypatch.delenv("AGENT_BRIDGE_PROJECT", raising=False)
    with pytest.raises(SystemExit):
        common.preamble()  # enabled, but not a bridged session
    monkeypatch.setenv("AAW_PROJECT", "proj")
    monkeypatch.setenv("AAW_AGENT", "grok")
    s, project, agent = common.preamble()
    assert (s.state_dir, project, agent) == (settings.state_dir, "proj", "grok")


def test_read_payload(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash"}'))
    assert common.read_payload() == {"tool_name": "Bash"}
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert common.read_payload() == {}
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2]"))
    assert common.read_payload() == {}


def test_mobile_mode_helpers(settings):
    assert not common.is_mobile_mode(settings)
    common.refresh_mobile_mode(settings)
    assert common.is_mobile_mode(settings)  # a timestamp
    settings.mobile_mode_file.write_text("manual")
    common.refresh_mobile_mode(settings)
    assert settings.mobile_mode_file.read_text() == "manual"  # manual is never overwritten
    settings.mobile_mode_file.write_text("0")
    assert not common.is_mobile_mode(settings)


def test_hook_log_and_tmux_session(settings):
    common.hook_log(settings, "x", "hello")
    assert "hello" in (settings.logs_dir / "x.log").read_text()
    assert common.tmux_session("my-app") == "cb-my-app"
    assert common.tmux_session("") == "claude"


def test_open_transport_needs_relay_and_identity(settings):
    assert common.open_transport(settings, "p") is None  # no relay url
    s2 = Settings(state_dir=settings.state_dir, relay_url="ws://x", computer_name="b", keep_awake=False)
    assert common.open_transport(s2, "p") is None  # no identity file yet


# ── on_pre_tool ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("agent,expected", [
    ("claude", {"decision": "approve"}),
    ("codex", {"hookSpecificOutput": {"permissionDecision": "approve"}}),
    ("gemini", {"decision": "allow"}),
    ("cursor", {"permission": "allow"}),
    ("scoot", {"permissionDecision": "allow"}),
])
def test_approve_output_per_agent(agent, expected):
    out, code = on_pre_tool.approve_output(agent)
    assert json.loads(out) == expected and code == 0


def test_block_output_per_agent():
    assert json.loads(on_pre_tool.block_output("claude")[0]) == {"decision": "block", "reason": "Denied from mobile app."}
    out, code = on_pre_tool.block_output("grok", "nope")
    assert json.loads(out) == {"decision": "block", "reason": "nope"} and code == 2  # Grok needs exit 2
    assert json.loads(on_pre_tool.block_output("gemini")[0])["decision"] == "deny"
    assert json.loads(on_pre_tool.block_output("cursor")[0]) == {"permission": "deny", "user_message": "Denied from mobile app."}
    assert json.loads(on_pre_tool.block_output("codex")[0])["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(on_pre_tool.block_output("scoot")[0])["permissionDecision"] == "deny"


def test_normalize_payload_dialects():
    grok = on_pre_tool.normalize_payload("grok", {"toolName": "run_terminal_cmd", "toolInput": {"command": "ls"}})
    assert grok["tool_name"] == "run_terminal_cmd" and grok["tool_input"] == {"command": "ls"}
    shell = on_pre_tool.normalize_payload("cursor", {"hook_event_name": "beforeShellExecution", "command": "rm -rf x"})
    assert shell["tool_name"] == "Shell" and shell["tool_input"] == {"command": "rm -rf x"}
    mcp = on_pre_tool.normalize_payload("cursor", {"hook_event_name": "beforeMCPExecution", "tool_name": "web_search",
                                                   "tool_input": json.dumps({"query": "aaw"})})
    assert mcp["tool_name"] == "MCP:web_search" and mcp["tool_input"] == {"query": "aaw"}
    bad = on_pre_tool.normalize_payload("cursor", {"hook_event_name": "beforeMCPExecution", "tool_name": "t",
                                                   "tool_input": "{not json"})
    assert bad["tool_input"] == {"raw": "{not json"}


def test_build_question():
    assert on_pre_tool.build_question("Write", {"file_path": "a.py"}) == "Create / overwrite: a.py"
    assert on_pre_tool.build_question("write_file", {}) == "Create / overwrite: a file"
    assert on_pre_tool.build_question("Edit", {"file_path": "a.py"}) == "Edit: a.py"
    assert on_pre_tool.build_question("Bash", {"command": "  pytest -q  "}) == "Run: pytest -q"
    assert on_pre_tool.build_question("run_terminal_cmd", {"command": "x" * 200}) == "Run: " + "x" * 140
    assert on_pre_tool.build_question("search_replace", {"path": "b.py"}) == "Edit: b.py"
    assert on_pre_tool.build_question("MCP:web_search", {"query": "aaw"}) == "Allow web_search? (aaw)"
    assert on_pre_tool.build_question("MCP:thing", {}) == "Allow thing?"
    assert on_pre_tool.build_question("Glob", {}) == "Allow Glob?"


def test_readonly_tools_are_approved_without_a_transport(settings):
    for agent, tool in (("gemini", "read_file"), ("grok", "web_search")):
        out, code = on_pre_tool.decide(settings, "p", agent, {"tool_name": tool, "tool_input": {}})
        assert code == 0 and json.loads(out) == json.loads(on_pre_tool.approve_output(agent)[0])
    # no relay configured: approve so the agent shows its own dialog path unchanged
    out, code = on_pre_tool.decide(settings, "p", "claude", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert json.loads(out) == {"decision": "approve"} and code == 0


# ── on_post_tool / on_user_prompt / on_notification ────────────────────────


def test_post_tool_summaries():
    assert on_post_tool.summarize("Write", {"file_path": "a.py"}) == "Wrote a.py"
    assert on_post_tool.summarize("Edit", {}) == "Edited file"
    assert on_post_tool.summarize("Shell", {"command": "npm test"}) == "Ran: npm test"
    assert on_post_tool.summarize("Bash", {"command": "x" * 70}) == "Ran: " + "x" * 60 + "…"
    assert on_post_tool.summarize("read_file", {"path": "b.py"}) == "Read b.py"
    assert on_post_tool.summarize("Grep", {}) == "Completed Grep"
    assert on_post_tool.normalize("grok", {"toolName": "Read"})["tool_name"] == "Read"


def test_user_prompt_filtering():
    assert on_user_prompt.user_prompt_from({"prompt": "  hi there "}, "claude") == "hi there"
    assert on_user_prompt.user_prompt_from({"user_prompt": "/clear"}, "claude") == ""
    assert on_user_prompt.user_prompt_from({"prompt": "<task-notification>x</task-notification>"}, "claude") == ""
    assert on_user_prompt.user_prompt_from({"prompt": "<user_query>fix it</user_query>"}, "grok") == "fix it"
    assert on_user_prompt.user_prompt_from({}, "claude") == ""


def test_grok_internal_notifications_are_dropped():
    assert on_notification.is_grok_internal("grok", "SessionNotification { session_id: SessionId(1) }")
    assert not on_notification.is_grok_internal("grok", "Waiting for your input")
    assert not on_notification.is_grok_internal("claude", "SessionNotification { x }")


# ── stop handlers ───────────────────────────────────────────────────────────


def test_gemini_run():
    assert on_stop_gemini.run({"hook_event_name": "AfterAgent", "prompt_response": " done ", "prompt": "do"}) == \
        ("done", "do", False, "")
    assert on_stop_gemini.run({"hook_event_name": "SessionEnd"}) == ("", "", True, "")


def test_cursor_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join([
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "q"}]}}),
        json.dumps({"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"},
                                                                 {"type": "text", "text": "first"}]}}),
        "garbage",
        json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "final"}]}}),
    ]))
    assert on_stop_cursor.response_from_transcript(str(t)) == "final"
    assert on_stop_cursor.run({"hook_event_name": "stop", "transcript_path": str(t)}) == ("final", "", False, "")
    assert on_stop_cursor.run({"hook_event_name": "sessionEnd"}) == ("", "", True, "")
    assert on_stop_cursor.response_from_transcript(str(tmp_path / "missing")) == ""


def test_claude_transcript_latest_entry_only(tmp_path):
    t = tmp_path / "c.jsonl"
    lines = [
        json.dumps({"type": "attachment"}),  # metadata, no role
        json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "mid-turn sentence"}]}}),
        json.dumps({"message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]}}),
    ]
    t.write_text("\n".join(lines))
    assert on_stop_claude.response_from_transcript(str(t)) == ""  # tool result just landed: turn not finished
    lines.append(json.dumps({"message": {"role": "assistant", "content": [{"type": "tool_use"},
                                                                          {"type": "text", "text": "the answer"}]}}))
    t.write_text("\n".join(lines))
    assert on_stop_claude.response_from_transcript(str(t)) == "the answer"
    assert on_stop_claude.api_error_kind_from_transcript(str(t)) == ("", "")
    lines.append(json.dumps({"isApiErrorMessage": True,
                             "message": {"role": "assistant", "content": "API Error: Login expired · run /login"}}))
    t.write_text("\n".join(lines))
    kind, text = on_stop_claude.api_error_kind_from_transcript(str(t))
    assert kind == "auth" and "Login expired" in text
    assert "sign back in" in on_stop_claude.friendly_api_error_message(kind, text)
    assert on_stop_claude.friendly_api_error_message("limit", "usage limit reached") == "usage limit reached"
    assert "too long" in on_stop_claude.friendly_api_error_message("prompt_too_long", "x")
    assert "interrupted" in on_stop_claude.friendly_api_error_message("connection", "ECONNRESET")


def test_stray_line_dump_guard():
    assert on_stop_claude.looks_like_stray_line_dump(["   91    def f():", "   92        pass"])
    assert not on_stop_claude.looks_like_stray_line_dump(["   1    first item", "   2    second"])  # a real list
    assert not on_stop_claude.looks_like_stray_line_dump(["2024 was a year", "  3   x"])
    assert not on_stop_claude.looks_like_stray_line_dump([])


def test_parse_reset_info():
    ts, label = on_stop_claude.parse_reset_info("You're out of usage. Limit resets 2pm (America/New_York).")
    assert ts > time.time() and label.startswith("2pm ")
    ts2, _ = on_stop_claude.parse_reset_info("resets at 11:30 am (Nowhere/Invalid)")
    assert ts2 > time.time()  # unknown zone falls back to UTC
    assert on_stop_claude.parse_reset_info("no reset info here") == (0, "")


def test_status_message_table():
    sm = on_stop.status_message
    assert sm(reason="ECONNRESET", stop_type="", api_error_text="", response_text="", is_final_stop=True,
              agent="claude")[1] == "error"
    assert sm(reason="", stop_type="error", api_error_text="API Error: x\nmore", response_text="",
              is_final_stop=True, agent="claude") == ("API Error: x", "warning")
    assert sm(reason="boom error", stop_type="", api_error_text="", response_text="", is_final_stop=True,
              agent="claude") == ("Session ended with error: boom error. Send 'restart' to try again.", "warning")
    assert sm(reason="", stop_type="", api_error_text="Login expired", response_text="Login expired",
              is_final_stop=False, agent="claude") == ("Login expired", "warning")
    assert sm(reason="", stop_type="", api_error_text="", response_text="Done.\ndetails", is_final_stop=False,
              agent="claude") == ("Response ready: Done.", "success")
    assert sm(reason="", stop_type="", api_error_text="", response_text="", is_final_stop=True,
              agent="codex") == ("Codex session ended. Send 'restart' to start a new session.", "success")
    assert sm(reason="", stop_type="", api_error_text="", response_text="", is_final_stop=False,
              agent="grok") == ("", "success")
    assert on_stop.is_error_stop("", "ECONNRESET") and not on_stop.is_error_stop("", "normal")


def test_grok_pane_extractor():
    pane = [
        "~/proj                                     12K / 128K",
        "█◆ user_prompt_submit",
        "◆ Thought for 2.1s",
        "Here is the plan.",
        "",
        "◆ stop tool_use",
        "And the rest of the answer.  4:36 PM",
        "⠧ Responding… 3s",
        "╭──────────╮",
        "│ ❯        │",
        "╰─ Grok Build ─╯",
    ]
    assert on_stop_grok.response_from_grok_pane(pane) == "Here is the plan.\nAnd the rest of the answer."
    # fallback: the prompt marker scrolled off; the input box confirms a turn boundary
    scrolled = ["old answer", "╭────╮", "│ ❯  │", "╰────╯", "◆ stop", "new answer", "⠋ Responding…"]
    assert on_stop_grok.response_from_grok_pane(scrolled) == "new answer"
    assert on_stop_grok.response_from_grok_pane(["◆ session_end"]) == ""
    assert on_stop_grok.is_chrome("") and on_stop_grok.is_chrome("~/proj") and on_stop_grok.is_chrome("Shift+Tab to cycle")
    assert not on_stop_grok.is_chrome("A real sentence.")
    assert on_stop_grok.grok_clean("▌hello▐ ") == "hello"


def test_scoot_model_prefers_env_then_newest_session_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOOT_MODEL", "ollama/qwen2.5")
    assert on_session_start.scoot_model() == "ollama/qwen2.5"
    monkeypatch.delenv("SCOOT_MODEL")
    state = tmp_path / "state"
    (state / "sessions").mkdir(parents=True)
    monkeypatch.setenv("SCOOT_STATE_DIR", str(state))
    monkeypatch.chdir(tmp_path)
    (state / "sessions" / "a.json").write_text(json.dumps({"root": str(tmp_path), "active_model": "openai/gpt"}))
    assert on_session_start.scoot_model() == "openai/gpt"
    (state / "sessions" / "b.json").write_text(json.dumps({"root": "/elsewhere", "model": "other"}))
    assert on_session_start.scoot_model() == "openai/gpt"  # another folder's session is ignored
