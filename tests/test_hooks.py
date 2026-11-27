"""The hooks' pure logic: per-agent output dialects, payload normalization, question
text, transcript parsing, the Grok pane extractor, the reset-time parser, the
stray-dump guard, the status-message table, and the shared helpers."""

from __future__ import annotations

import io
import json
import os
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
    # the commercial host's variables never count: its sessions must not run these hooks
    monkeypatch.setenv("AGENT_BRIDGE_PROJECT", "theirs")
    monkeypatch.setenv("AGENT_BRIDGE_AGENT", "codex")
    assert common.env_project() == "" and common.env_agent() == "claude"
    monkeypatch.setenv("AAW_PROJECT", "ours")
    monkeypatch.setenv("AAW_AGENT", "gemini")
    assert common.env_project() == "ours" and common.env_agent() == "gemini"


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
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s1"}'))
    s, project, agent = common.preamble()
    assert (s.state_dir, project, agent) == (settings.state_dir, "proj", "grok")
    # preamble read stdin for the session id; read_payload hands that payload out once
    assert common.read_payload() == {"session_id": "s1"}


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
    assert common.tmux_session("my-app") == "aaw-my-app"
    assert common.tmux_session("") == "claude"


def test_open_transport_needs_relay_and_identity(settings):
    assert common.open_transport(settings, "p") is None  # no relay url
    s2 = Settings(state_dir=settings.state_dir, relay_url="ws://x", computer_name="b", keep_awake=False)
    assert common.open_transport(s2, "p") is None  # no identity file yet


# ── on_pre_tool ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("agent,expected", [
    ("claude", {"decision": "approve"}),
    ("gemini", {"decision": "allow"}),
    ("cursor", {"permission": "allow"}),
    ("scoot", {"permissionDecision": "allow"}),
])
def test_approve_output_per_agent(agent, expected):
    out, code = on_pre_tool.approve_output(agent)
    assert json.loads(out) == expected and code == 0


def test_codex_is_approved_by_saying_nothing():
    """2026-11-26: Codex (app-server daemon 0.172) printed "Hook failed: PreToolUse hook
    returned unsupported permissionDecision:allow" after every approval from the phone. It
    acts on deny only; an empty reply lets the call go on to its own rules."""
    assert on_pre_tool.approve_output("codex") == ("", 0)


def test_block_output_per_agent():
    assert json.loads(on_pre_tool.block_output("claude")[0]) == {"decision": "block", "reason": "Denied from mobile app."}
    out, code = on_pre_tool.block_output("grok", "nope")
    assert json.loads(out) == {"decision": "block", "reason": "nope"} and code == 2  # Grok needs exit 2
    assert json.loads(on_pre_tool.block_output("gemini")[0])["decision"] == "deny"
    assert json.loads(on_pre_tool.block_output("cursor")[0]) == {"permission": "deny", "user_message": "Denied from mobile app."}
    # Codex 0.162 reads the reply against a schema: without the event's name it is invalid,
    # the hook counts as failed, and the denied command runs (reproduced 2026-11-21).
    assert json.loads(on_pre_tool.block_output("codex", "Denied.")[0]) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Denied."}}
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
    # scoot's own tool names and its "path" argument
    assert on_pre_tool.build_question("run_shell", {"command": " ls -la "}) == "Run: ls -la"
    assert on_pre_tool.build_question("write_file", {"path": "a.py", "content": "x"}) == "Create / overwrite: a.py"
    assert on_pre_tool.build_question("edit_file", {"path": "b.py"}) == "Edit: b.py"
    assert on_pre_tool.build_question("open_editor", {"path": "c.py"}) == "Open in the editor: c.py"
    assert on_pre_tool.build_question("web_search", {"query": " aaw\n app ", "count": 3}) == "Search the web: aaw app"
    assert on_pre_tool.build_question("web_search", {}) == "Search the web?"


def test_readonly_tools_are_approved_without_a_transport(settings):
    for agent, tool in (("gemini", "read_file"), ("grok", "web_search"), ("scoot", "read_file"), ("scoot", "search")):
        out, code = on_pre_tool.decide(settings, "p", agent, {"tool_name": tool, "tool_input": {}})
        assert code == 0 and json.loads(out) == json.loads(on_pre_tool.approve_output(agent)[0])
    # no relay configured: approve so the agent shows its own dialog path unchanged
    out, code = on_pre_tool.decide(settings, "p", "claude", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert json.loads(out) == {"decision": "approve"} and code == 0


class _NoAnswerTransport:
    """A transport whose question is never answered; records what decide() does about it."""

    def __init__(self):
        self.question_kwargs = None
        self.status_calls = []
        self.notifications = []

    def get_project(self):
        return {}

    def send_question(self, **kwargs):
        self.question_kwargs = kwargs

    def poll_answer(self, deadline):
        return None  # the 5 minutes pass with nobody answering

    def set_project_status(self, status, last_event_summary="", pending_question_id=None):
        self.status_calls.append((status, pending_question_id))

    def write_notification(self, message, level, set_running=False):
        self.notifications.append((message, level))

    def update_project(self, **fields):
        pass

    def stop(self):
        pass


def test_an_unanswered_question_clears_itself_and_leaves_a_trace(settings, monkeypatch):
    """A poll_answer timeout must not leave a zombie card: the stale pending_question_id
    stalled the next question for a full PENDING_WAIT_S (seen live 2026-09-30), and the
    default-approve left no trace in the feed."""
    transport = _NoAnswerTransport()
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: transport)
    out, code = on_pre_tool.decide(settings, "p", "claude",
                                   {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert json.loads(out) == {"decision": "approve"} and code == 0  # timeout still default-approves
    assert ("running", "") in transport.status_calls  # the card dismisses and the queue unblocks
    assert transport.notifications, "a silent default-approve: the feed must say what happened"
    message, level = transport.notifications[0]
    assert "No answer in 5 minutes" in message and "approved and continued" in message
    assert level == "info"  # feed-only, never a push
    # the card's countdown must not promise more time than the hook will wait
    assert transport.question_kwargs.get("timeout_s") == on_pre_tool.ANSWER_TIMEOUT_S


class _AnsweringTransport(_NoAnswerTransport):
    """A transport with a project doc and a phone that answers."""

    def __init__(self, doc=None, answer=None):
        super().__init__()
        self.doc = doc or {}
        self.answer = answer
        self.updates = []

    def get_project(self):
        return self.doc

    def poll_answer(self, deadline):
        return self.answer

    def update_project(self, **fields):
        self.updates.append(fields)


_SCOOT_SEARCH = {"tool_name": "web_search", "tool_input": {"query": "aaw"}}


def test_scoot_web_search_is_asked_on_every_call(settings, monkeypatch):
    """scoot 0.15.0 never auto-approves web_search, and takes a global hook's "allow" as the
    user's answer. Seen 2026-11-20 with 0.1.19: a bridged scoot ran the search with nobody
    asked. The session's auto-approve must not cover it, and the card offers one call only."""
    transport = _AnsweringTransport(doc={"auto_approve": True}, answer="Yes")
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: transport)
    out, code = on_pre_tool.decide(settings, "p", "scoot", dict(_SCOOT_SEARCH))
    assert transport.question_kwargs is not None, "auto-approve answered a tool that always asks"
    assert transport.question_kwargs["question"] == "Search the web: aaw"
    assert transport.question_kwargs["options"] == ["Yes", "No"]
    assert transport.question_kwargs["kind"] == "choice"  # pushed even while the session auto-approves
    assert json.loads(out) == {"permissionDecision": "allow"} and code == 0
    assert transport.updates == []  # one "Yes" trusts nothing

    # the same session still auto-approves an ordinary tool without asking
    other = _AnsweringTransport(doc={"auto_approve": True})
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: other)
    out, _ = on_pre_tool.decide(settings, "p", "scoot", {"tool_name": "run_shell", "tool_input": {"command": "ls"}})
    assert other.question_kwargs is None and json.loads(out) == {"permissionDecision": "allow"}
    asked = _AnsweringTransport(answer="Yes")
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: asked)
    on_pre_tool.decide(settings, "p", "scoot", {"tool_name": "run_shell", "tool_input": {"command": "ls"}})
    assert asked.question_kwargs["kind"] == "permission" and asked.question_kwargs["options"] == on_pre_tool.OPTIONS

    # an old phone's "Yes for this session" approves this call and does not switch auto-approve on
    stale = _AnsweringTransport(answer="Yes for this session")
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: stale)
    out, _ = on_pre_tool.decide(settings, "p", "scoot", dict(_SCOOT_SEARCH))
    assert json.loads(out) == {"permissionDecision": "allow"} and stale.updates == []

    denied = _AnsweringTransport(answer="No")
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: denied)
    out, _ = on_pre_tool.decide(settings, "p", "scoot", dict(_SCOOT_SEARCH))
    assert json.loads(out)["permissionDecision"] == "deny"


def test_scoot_web_search_is_not_run_when_nobody_answers(settings, monkeypatch):
    transport = _NoAnswerTransport()
    monkeypatch.setattr(on_pre_tool, "open_transport", lambda s, p: transport)
    out, code = on_pre_tool.decide(settings, "p", "scoot", dict(_SCOOT_SEARCH))
    assert json.loads(out)["permissionDecision"] == "deny" and code == 0
    assert ("running", "") in transport.status_calls  # the card still dismisses
    message, level = transport.notifications[0]
    assert "No answer in 5 minutes; not run: Search the web: aaw" == message and level == "info"


def test_scoot_web_search_without_a_relay_is_left_to_scoot(settings):
    """Not linked: the hook says nothing, so scoot shows its own prompt (it always asks)."""
    assert on_pre_tool.decide(settings, "p", "scoot", dict(_SCOOT_SEARCH)) == ("", 0)
    # the name means nothing for another agent: Grok's web_search stays read-only
    out, _ = on_pre_tool.decide(settings, "p", "grok", dict(_SCOOT_SEARCH))
    assert json.loads(out) == json.loads(on_pre_tool.approve_output("grok")[0])


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
    assert on_user_prompt.user_prompt_from(
        {"prompt": '<agent-message from="a1b2">[Subagent hand-back] report text</agent-message>'}, "claude") == ""
    assert on_user_prompt.user_prompt_from({"prompt": "<user_query>fix it</user_query>"}, "grok") == "fix it"
    assert on_user_prompt.user_prompt_from({}, "claude") == ""


def test_claude_paste_markers_are_stripped_from_prompts():
    # Claude Code wraps each terminal paste in <pasted_content id="..."> ... </pasted_content id="...">,
    # a random id per paste, repeated on the closing tag (seen 2026-10-18 on the phone as raw tags).
    one = '<pasted_content id="d3a9">\n(base) % photopush ~/Pictures\n33171 assets\n</pasted_content id="d3a9">'
    assert on_user_prompt.user_prompt_from({"prompt": one}, "claude") == "(base) % photopush ~/Pictures\n33171 assets"
    # Typed text around two pastes survives; every pair goes, not only a leading one.
    two = ('check this:\n<pasted_content id="a1">\nfirst block\n</pasted_content id="a1">\n'
           'and this:\n<pasted_content id="b2">second block</pasted_content id="b2">\nthanks')
    assert on_user_prompt.user_prompt_from({"prompt": two}, "claude") == (
        "check this:\nfirst block\nand this:\nsecond block\nthanks")
    # A bare closing tag and a prompt with no paste are both fine.
    assert on_user_prompt.strip_paste_markers('<pasted_content id="x">y</pasted_content>') == "y"
    assert on_user_prompt.strip_paste_markers("plain text") == "plain text"
    # The pasted block's own blank lines stay; only the runs the wrappers leave collapse.
    assert on_user_prompt.strip_paste_markers('<pasted_content id="q">\n\na\n\nb\n\n</pasted_content id="q">') == "a\n\nb"


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
    assert "Restart" in on_stop_claude.friendly_api_error_message("connection", "ECONNRESET")


def test_model_from_transcript_per_agent(tmp_path):
    """The phone shows the model next to the agent. Claude's assistant entries name it;
    a Codex rollout names it in turn_context (and thread_settings_applied at start). The
    newest entry wins, so a /model switch shows after the next turn."""
    t = tmp_path / "claude.jsonl"
    t.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5", "content": []}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-fable-5-1", "content": []}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>", "content": []}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        "not json",
    ]))
    assert on_stop_claude.model_from_transcript(str(t)) == "claude-fable-5-1"
    c = tmp_path / "rollout.jsonl"
    c.write_text("\n".join([
        json.dumps({"type": "event_msg", "payload": {"type": "thread_settings_applied",
                                                     "thread_settings": {"model": "gpt-5.1"}}}),
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-sol", "cwd": "/x"}}),
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant"}}),
    ]))
    assert on_stop_claude.model_from_transcript(str(c), "codex") == "gpt-6-sol"
    assert on_stop_claude.model_from_transcript(str(tmp_path / "missing")) == ""
    assert on_stop_claude.model_from_transcript("") == ""


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


class _ModelTransport:
    def __init__(self):
        self.updates: list[dict] = []
        self.cleared = False

    def update_project(self, **fields):
        self.updates.append(fields)

    def clear_events(self):
        self.cleared = True

    def stop(self):
        pass


def test_session_start_restamps_or_clears_the_model_for_claude_and_codex(settings, monkeypatch, tmp_path):
    """A folder that ran scoot kept scoot's model on the doc, and the Claude session that
    replaced it showed "gpt-6-astra" for days. A resumed session names its model in the
    transcript; a fresh one has none and the first turn's Stop hook fills it in."""
    t = tmp_path / "resumed.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5"}}))
    calls = []

    def run(agent, payload):
        transport = _ModelTransport()
        monkeypatch.setattr(on_session_start, "preamble", lambda: (settings, "p", agent))
        monkeypatch.setattr(on_session_start, "read_payload", lambda: payload)
        monkeypatch.setattr(on_session_start, "open_transport", lambda s, p: transport)
        assert on_session_start.main() == 0
        calls.append(transport)
        return transport

    assert run("claude", {"source": "resume", "transcript_path": str(t)}).updates == [{"model": "claude-opus-5-5"}]
    assert run("claude", {"source": "startup", "transcript_path": str(tmp_path / "fresh.jsonl")}).updates == [{"model": ""}]
    assert run("codex", {"source": "startup"}).updates == [{"model": ""}]
    assert run("claude", {"source": "compact", "transcript_path": str(t)}).updates == []  # mid-session: untouched
    cleared = run("claude", {"source": "clear", "transcript_path": str(t)})
    assert cleared.cleared and cleared.updates == []
    assert run("gemini", {"source": "startup"}).updates == []  # no transcript model for gemini yet


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


def test_notify_honours_local_notifications_off(tmp_path, monkeypatch):
    from aaw_core.config import Settings
    from aaw_core.hooks import common
    shown = []
    monkeypatch.setattr(common, "desktop_banner", lambda title, msg: shown.append(msg))
    on = Settings(state_dir=tmp_path, relay_url=None, computer_name="x", keep_awake=False, local_notifications=True)
    off = Settings(state_dir=tmp_path, relay_url=None, computer_name="x", keep_awake=False, local_notifications=False)
    common.notify(None, "Response ready", "success", settings=on)
    common.notify(None, "Response ready", "success", settings=off)
    assert shown == ["\u2705 Response ready"] or len(shown) == 1


# ── nested agents: an agent the session's agent started is not the session ───────────

START = "Sat Nov 14 13:15:36 2026"


def _table(*rows):
    """pid -> (ppid, start, command) from (pid, ppid, command) rows."""
    return {pid: (ppid, START, command) for pid, ppid, command in rows}


# The session's own Claude (11183) under the tmux pane's shell, its hook (500) started
# through sh; and a `claude -p` (700) that a script run by its Bash tool started.
OWN = [(1, 0, "/sbin/launchd"), (11181, 1, "zsh"), (11183, 11181, "claude"),
       (499, 11183, "/bin/sh"), (500, 499, "python3")]
NESTED = [*OWN, (600, 11183, "/bin/zsh"), (650, 600, "python3"), (700, 650, "claude"),
          (799, 700, "/bin/sh"), (800, 799, "python3")]


def test_firing_agent_skips_shells():
    from aaw_core.hooks import owner

    table = _table(*NESTED)
    assert owner.firing_agent(table, 500) == 11183
    assert owner.firing_agent(table, 800) == 700
    assert owner.firing_agent(table, 4242) == 0  # not in the table
    assert owner.is_ancestor(table, 11183, 700) and not owner.is_ancestor(table, 700, 11183)
    assert not owner.is_ancestor(table, 11183, 11183)


def test_nested_agent_hooks_are_ignored(settings):
    from aaw_core.hooks import owner

    table = _table(*NESTED)

    def nested(pid, session, agent="claude"):
        return owner.nested_agent(settings, "kids", agent, {"session_id": session}, table=table, pid=pid)

    assert nested(500, "own-1") == ""  # the first agent to speak owns the session
    assert json.loads(owner.owner_file(settings, "kids").read_text()) == {
        "pid": 11183, "start": START, "sessions": ["own-1"]}
    assert "pid 700 (claude)" in nested(800, "child-1")  # the script's claude -p
    assert nested(500, "own-2") == ""  # the owner after /clear: a new conversation id
    assert nested(500, "own-1") == "" and nested(500, "own-2") == ""
    assert "session child-2" in nested(800, "child-2")
    # A helper process of the owner that reports the owner's conversation is the owner.
    assert nested(800, "own-2") == ""
    # Agents whose process layout was never checked keep the old behavior.
    for unchecked in ("codex", "grok", "cursor", "scoot"):
        assert nested(800, "child-3", agent=unchecked) == ""
    # No session id in the payload: not enough to call it nested.
    assert owner.nested_agent(settings, "kids", "claude", {}, table=table, pid=800) == ""


def test_nested_agent_owner_changes(settings):
    from aaw_core.hooks import owner

    def nested(table, pid, session):
        return owner.nested_agent(settings, "kids", "claude", {"session_id": session}, table=table, pid=pid)

    # Upgrade mid-turn: the nested agent spoke first and was taken for the owner; the real
    # owner's next hook takes the session back.
    table = _table(*NESTED)
    assert nested(table, 800, "child-1") == ""
    assert nested(table, 500, "own-1") == ""
    assert json.loads(owner.owner_file(settings, "kids").read_text())["pid"] == 11183
    assert nested(table, 800, "child-1") != ""
    # The session restarted: the remembered owner is gone, or its pid belongs to a process
    # with another start time. The new agent owns the session.
    restarted = _table((1, 0, "/sbin/launchd"), (21181, 1, "zsh"), (21183, 21181, "claude"), (900, 21183, "python3"))
    assert nested(restarted, 900, "new-1") == ""
    assert json.loads(owner.owner_file(settings, "kids").read_text())["pid"] == 21183
    reused = {**restarted, 21183: (21181, "Sun Nov 15 09:00:00 2026", "claude")}
    assert nested(reused, 900, "new-2") == ""
    assert json.loads(owner.owner_file(settings, "kids").read_text())["sessions"] == ["new-2"]
    # ps unreadable: nothing is dropped.
    assert nested({}, 900, "whatever") == ""


def test_process_table_reads_this_process():
    from aaw_core.hooks import owner

    table = owner.process_table()
    parent, start, command = table[os.getpid()]
    assert parent == os.getppid() and len(start.split()) == 5 and command


def test_preamble_drops_a_nested_agents_hook(monkeypatch, settings):
    from aaw_core.hooks import owner

    monkeypatch.setenv("AAW_STATE_DIR", str(settings.state_dir))
    monkeypatch.setenv("AAW_PROJECT", "kids")
    monkeypatch.setenv("AAW_AGENT", "claude")
    settings.enabled_flag.parent.mkdir(parents=True, exist_ok=True)
    settings.enabled_flag.write_text("")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "child-1"}'))
    monkeypatch.setattr(owner, "nested_agent", lambda *a, **k: "pid 700 (claude)")
    with pytest.raises(SystemExit) as e:
        common.preamble()
    assert e.value.code == 0
    assert "ignored" in (settings.logs_dir / "nested.log").read_text()
    common.read_payload()  # leave no cached payload behind for the next test


# ── Codex: the session comes from the hook's own input ───────────────────────────────────


def _codex_rollout(tmp_path, name, originator="codex-tui", source="cli"):
    path = tmp_path / f"rollout-2026-11-26T23-28-10-{name}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"originator": originator, "source": source}}) + "\n")
    return str(path)


def _mirror(settings, project, **doc):
    d = settings.state_dir / "projects"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{project}.json").write_text(json.dumps(doc))


def test_a_codex_hook_finds_its_session_by_folder_not_by_environment(settings, tmp_path):
    """2026-11-26: Codex fires every hook from one daemon shared by all its sessions, with the
    environment of the session that started it (AAW_PROJECT=x-codex). A second session's
    prompt and approval card went to x-codex; its own feed stayed empty."""
    from aaw_core.hooks import owner

    first, second = tmp_path / "x", tmp_path / "repro"
    first.mkdir(), second.mkdir()
    _mirror(settings, "x-codex", agent="codex", status="idle", project_path=str(first))
    _mirror(settings, "nested-repro", agent="codex", status="running", project_path=str(second))
    _mirror(settings, "repro-claude", agent="claude", status="running", project_path=str(second))
    payload = {"session_id": "s1", "cwd": str(second), "transcript_path": _codex_rollout(tmp_path, "a")}

    assert owner.is_codex_hook(payload, "claude") and owner.is_codex_hook({}, "codex")
    assert not owner.is_codex_hook({}, "claude")
    assert not owner.is_codex_hook({"transcript_path": "/x/.claude/projects/p/abc.jsonl"}, "codex")
    assert owner.codex_session(settings, payload, "x-codex") == ("nested-repro", "")
    # The daemon was started outside any bridged session: no environment at all.
    assert owner.codex_session(settings, payload, "") == ("nested-repro", "")
    # A folder reached through a symlink is the same folder.
    link = tmp_path / "link"
    link.symlink_to(second)
    assert owner.codex_session(settings, {**payload, "cwd": str(link)}, "x-codex") == ("nested-repro", "")
    # A Codex conversation in a folder with no bridged Codex session belongs to nobody,
    # whatever the daemon's environment says.
    other = tmp_path / "other"
    other.mkdir()
    assert owner.codex_session(settings, {**payload, "cwd": str(other)}, "x-codex") == ("", "")
    assert owner.codex_session(settings, {**payload, "cwd": str(other)}, "repro-claude") == ("", "")


def test_a_codex_hook_keeps_the_environment_when_the_record_cannot_say(settings, tmp_path):
    from aaw_core.hooks import owner

    payload = {"session_id": "s1", "cwd": str(tmp_path), "transcript_path": _codex_rollout(tmp_path, "a")}
    # No record yet (the first hook can beat the daemon's first write), or one with no folder.
    assert owner.codex_session(settings, payload, "fresh") == ("fresh", "")
    _mirror(settings, "old", agent="codex", status="running")
    assert owner.codex_session(settings, payload, "old") == ("old", "")
    # No folder in the hook's input: nothing to look up, the environment stands.
    _mirror(settings, "proj", agent="codex", status="running", project_path=str(tmp_path))
    assert owner.codex_session(settings, {"session_id": "s1"}, "proj") == ("proj", "")
    # A stopped record of the same folder does not hide the environment's session.
    _mirror(settings, "proj", agent="codex", status="stopped", project_path=str(tmp_path))
    assert owner.codex_session(settings, payload, "proj") == ("proj", "")


def test_codex_exec_inside_a_codex_session_is_not_the_session(settings, tmp_path):
    from aaw_core.hooks import owner

    _mirror(settings, "proj", agent="codex", status="running", project_path=str(tmp_path))
    exec_payload = {"session_id": "child-123456", "cwd": str(tmp_path),
                    "transcript_path": _codex_rollout(tmp_path, "b", "codex_exec", "exec")}
    project, why = owner.codex_session(settings, exec_payload, "proj")
    assert project == "proj" and "codex exec conversation child-12" in why
    # The app-server daemon reports the interactive agent as "vscode"; that is the session.
    tui = {**exec_payload, "transcript_path": _codex_rollout(tmp_path, "c", "codex-tui", "vscode")}
    assert owner.codex_session(settings, tui, "proj") == ("proj", "")
    # A record that cannot be read counts as the session's own agent.
    broken = tmp_path / "rollout-broken.jsonl"
    broken.write_text("not json\n")
    assert owner.codex_session(settings, {**exec_payload, "transcript_path": str(broken)}, "proj") == ("proj", "")


def test_another_agent_under_a_codex_environment_is_ignored():
    from aaw_core.hooks import owner

    claude = {"session_id": "c1", "transcript_path": "/Users/me/.claude/projects/p/0b1e.jsonl"}
    assert "another agent's conversation" in owner.foreign_agent(claude, "codex")
    assert owner.foreign_agent(claude, "claude") == ""
    assert owner.foreign_agent({"session_id": "c1"}, "codex") == ""
    assert owner.foreign_agent({"transcript_path": "/x/rollout-1.jsonl"}, "codex") == ""


def test_preamble_routes_a_codex_hook_to_the_session_of_its_folder(monkeypatch, settings, tmp_path):
    monkeypatch.setenv("AAW_STATE_DIR", str(settings.state_dir))
    settings.enabled_flag.parent.mkdir(parents=True, exist_ok=True)
    settings.enabled_flag.write_text("")
    _mirror(settings, "nested-repro", agent="codex", status="running", project_path=str(tmp_path))
    monkeypatch.setenv("AAW_PROJECT", "x-codex")
    monkeypatch.setenv("AAW_AGENT", "codex")
    payload = {"session_id": "s1", "cwd": str(tmp_path), "transcript_path": _codex_rollout(tmp_path, "a")}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    _, project, agent = common.preamble()
    assert (project, agent) == ("nested-repro", "codex")
    # codex exec from that session: silent, and logged.
    payload["transcript_path"] = _codex_rollout(tmp_path, "b", "codex_exec", "exec")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as e:
        common.preamble()
    assert e.value.code == 0
    assert "codex exec conversation" in (settings.logs_dir / "nested.log").read_text()
    # A Codex hook with no environment at all still finds its session.
    monkeypatch.delenv("AAW_PROJECT")
    monkeypatch.delenv("AAW_AGENT")
    payload["transcript_path"] = _codex_rollout(tmp_path, "a")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert common.preamble()[1:] == ("nested-repro", "codex")


def test_gemini_run_by_a_gemini_session_is_ignored(settings):
    """2026-11-26, Gemini CLI 0.70: a session asked to run `gemini -p "..."` got the nested
    answer in its feed, "Response ready" for it, and "Gemini session ended" while the real
    session was still working. The layout seen: launcher 17930, its worker 19072 (fires the
    session's hooks), the shell tool's two bash processes, then the nested gemini 20964."""
    from aaw_core.hooks import owner

    table = _table((74788, 1, "tmux"), (17928, 74788, "zsh"), (17930, 17928, "node"), (19072, 17930, "node"),
                   (19910, 19072, "python3"),
                   (20956, 19072, "/bin/bash"), (20963, 20956, "/bin/bash"), (20964, 20963, "node"),
                   (21471, 20964, "python3"))

    def nested(pid, session):
        return owner.nested_agent(settings, "nested-gem", "gemini", {"session_id": session}, table=table, pid=pid)

    assert nested(19910, "own") == ""
    assert json.loads(owner.owner_file(settings, "nested-gem").read_text())["pid"] == 19072
    assert "pid 20964 (node), session child" in nested(21471, "child")
    assert nested(19910, "own") == ""


def test_a_hook_orphaned_to_init_never_becomes_the_owner(settings):
    """2026-11-26: Gemini's first hook ran after the process that started it had exited, so
    its parent was pid 1. Init became the owner, and every later hook of the session, its own
    approval question included, was dropped as "under owner pid 1"."""
    from aaw_core.hooks import owner

    table = _table((1, 0, "/sbin/launchd"), (300, 1, "python3"), (17930, 1, "node"), (19072, 17930, "node"),
                   (19910, 19072, "python3"))
    assert owner.nested_agent(settings, "g", "gemini", {"session_id": "s0"}, table=table, pid=300) == ""
    assert not owner.owner_file(settings, "g").exists()
    assert owner.nested_agent(settings, "g", "gemini", {"session_id": "s1"}, table=table, pid=19910) == ""
    assert json.loads(owner.owner_file(settings, "g").read_text())["pid"] == 19072
