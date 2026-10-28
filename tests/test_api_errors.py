"""API errors and false alerts, from the 2026-10-26 incidents and live Claude Code 2.1.312
captures (2026-10-28): a turn that dies on an API error fires StopFailure, prompts typed
during an API retry are queued (not undelivered), only Claude's own status line is a retry
banner, a stale "API Error" on screen never overrides a real answer, and slash-command
output is never a refused prompt."""

from __future__ import annotations

import time

import pytest

from aaw_core import daemon
from aaw_core.config import Settings
from aaw_core.hooks import on_stop, on_stop_claude, on_stop_failure
from aaw_core.host import detect

RULE = "─" * 120

# Claude Code 2.1.312 with its API unreachable, a second prompt typed during the retry.
RETRY_TYPED = f"""\
❯ say hi
✻ Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED) · Retrying in 8s · attempt 6/10
{RULE}
❯ Status
{RULE}
  ⏵⏵ auto mode on (shift+tab to cycle)
"""

# The same pane right after Enter: the prompt is queued, the input box shows a placeholder.
RETRY_QUEUED = f"""\
❯ say hi
❯ Status
  ctrl+x ctrl+s to send now
✻ Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED) · Retrying in 7s · attempt 6/10
{RULE}
❯ Press up to edit queued messages
{RULE}
  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← 1 agent
"""

# 2026-10-26: a session that printed another session's pane carried that pane's banner
# in its own tool output, behind the other pane's name.
BANNER_IN_TOOL_OUTPUT = f"""\
⏺ Bash(tmux capture-pane -p -t aaw-ugreen-nas | tail -3)
  ⎿  aaw-ugreen-nas               ✻ Unable to connect to API (EPROTO) · Retrying in 5s · attempt 3/10
✻ Crunching… (12s · esc to interrupt)
{RULE}
❯
{RULE}
"""

# The menu the phone answered on 2026-10-28, and the same menu with the cursor moved.
MODEL_MENU = f"""\
✻ Brewed for 9s
❯ /model
{RULE}
  Select model
  Switch between Claude models. Your pick becomes the default for new sessions.

  ❯ 1. Default (recommended)  Opus 5.5 · Best for everyday, complex tasks
    2. Fable 5.1              For your toughest challenges
    3. Sonnet 5.5             Most efficient for simpler tasks

  Enter to set as default · s to use this session only · Esc to cancel
"""
MODEL_MENU_MOVED = (MODEL_MENU
                    .replace("  ❯ 1. Default (recommended)  Opus", "    1. Default (recommended)  Opus")
                    .replace("    2. Fable 5.1              For", "  ❯ 2. Fable 5.1            For"))

# What the slash command printed under its own echo (2026-10-28, sent from the phone).
SLASH_OUTPUT = f"""\
❯ /model
  ⎿  Set model to Fable 5.1 and saved as your default for new sessions

{RULE}
❯
{RULE}
"""

# Claude Code 2.1.312's StopFailure payloads, verbatim but for the paths.
AUTH_FAILURE = {"hook_event_name": "StopFailure", "error": "authentication_failed",
                "last_assistant_message": "Please run /login · API Error: 401 Invalid bearer token"}
CONNECTION_FAILURE = {"hook_event_name": "StopFailure", "error": "server_error",
                      "last_assistant_message": "API Error: Connection refused — a firewall or proxy may be "
                                                "blocking it (ECONNREFUSED)"}


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)


class _Transport:
    project_id = "proj"

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, str]] = []
        self.status: list[str] = []

    def write_event(self, kind, payload, via=None):
        self.events.append((kind, payload))

    def write_notification(self, message, level, set_running=False):
        self.notifications.append((level, message))

    def set_project_status(self, status, **fields):
        self.status.append(status)

    def stop(self, *a, **k):
        pass


# ── a prompt typed during an API retry is queued, not undelivered ─────────────


def test_a_queued_prompt_counts_as_delivered():
    assert detect.pane_input_is_empty(RETRY_TYPED) is False  # typed, Enter not yet taken
    assert detect.pane_input_is_empty(RETRY_QUEUED) is True  # taken: queued behind the retry


# ── only Claude's own status line is a retry banner ───────────────────────────


def test_the_live_retry_banner_is_read():
    for pane in (RETRY_TYPED, RETRY_QUEUED):
        assert detect.detect_claude_retry(pane) == {
            "message": "Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)",
            "attempt": 6, "total": 10}


def test_a_banner_in_tool_output_is_not_a_retry():
    assert detect.detect_claude_retry(BANNER_IN_TOOL_OUTPUT) is None
    assert detect.detect_claude_retry("✻ Unable to connect · Retrying in 5s · attempt 3/10\n") is None  # no input box


# ── a menu's options read as one line each, and its cursor is not a new menu ──


def test_menu_options_lose_their_column_padding():
    menu = detect.detect_permission_prompt(MODEL_MENU)
    assert menu["menu"] and menu["question"] == "Select model"
    assert menu["options"] == ["Default (recommended) · Opus 5.5 · Best for everyday, complex tasks",
                               "Fable 5.1 · For your toughest challenges",
                               "Sonnet 5.5 · Most efficient for simpler tasks"]
    moved = detect.detect_permission_prompt(MODEL_MENU_MOVED)
    assert moved["options"] == menu["options"] and moved["hash"] == menu["hash"]
    assert moved["cursor"] == 1


# ── refusals: slash-command output and unknown text never alert ───────────────


def test_slash_command_output_is_not_a_refusal(settings):
    t = _Transport()
    watch = {"text": "/model", "ts": time.monotonic()}
    daemon.watch_for_refusal(t, settings, "claude", SLASH_OUTPUT, watch)
    assert watch == {} and t.notifications == []


def test_text_outside_the_error_families_is_not_a_refusal(settings):
    t = _Transport()
    pane = f"❯ list them\n  ⎿  17 skills available\n\n{RULE}\n❯ \n{RULE}\n"
    watch = {"text": "list them", "ts": time.monotonic()}
    daemon.watch_for_refusal(t, settings, "claude", pane, watch)
    assert t.notifications == []


def test_an_expired_login_pushes_once_per_session(settings):
    t = _Transport()
    pane = f"❯ Hey\n  ⎿  Login expired · Please run /login\n\n{RULE}\n❯ \n{RULE}\n"
    for prompt in ("Hey", "Hey"):
        daemon.watch_for_refusal(t, settings, "claude", pane, {"text": prompt, "ts": time.monotonic()})
    assert len(t.notifications) == 1 and "/login" in t.notifications[0][1]
    assert any(kind == "message" for kind, _ in t.events)  # the second one is still in the feed


# ── the error families ─────────────────────────────────────────────────────────


def test_login_wordings_are_auth():
    for text in ("Login expired · Please run /login", "Please run /login · API Error: 401 Invalid bearer token",
                 "Invalid API key · Please run /login"):
        assert on_stop_claude.api_error_kind_from_text(text) == "auth", text
    assert on_stop_claude.api_error_kind_from_text("API Error: Unable to connect to API (EPROTO)") == ""


def test_stop_failure_kinds():
    k = on_stop_claude.api_error_kind_from_stop_failure
    assert k("authentication_failed", AUTH_FAILURE["last_assistant_message"]) == "auth"
    assert k("server_error", CONNECTION_FAILURE["last_assistant_message"]) == "connection"
    assert k("rate_limit", "whatever") == "limit"
    assert k("", "Login expired · Please run /login") == "auth"  # an older Claude without the field
    msg = on_stop_claude.friendly_api_error_message("connection", CONNECTION_FAILURE["last_assistant_message"])
    assert "Restart" in msg and "not a sign-in issue" in msg


# ── the StopFailure hook ───────────────────────────────────────────────────────


def _run_stop_failure(monkeypatch, settings, payload):
    t = _Transport()
    monkeypatch.setattr(on_stop_failure, "preamble", lambda: (settings, "proj", "claude"))
    monkeypatch.setattr(on_stop_failure, "read_payload", lambda: payload)
    monkeypatch.setattr(on_stop_failure, "open_transport", lambda s, p: t)
    monkeypatch.setattr(on_stop_failure, "notify", lambda tr, msg, level, **k: tr.write_notification(msg, level))
    assert on_stop_failure.main() == 0
    return t


def test_stop_failure_reports_an_expired_login_once(monkeypatch, settings):
    first = _run_stop_failure(monkeypatch, settings, AUTH_FAILURE)
    assert first.notifications and first.notifications[0][0] == "error"
    assert "/login" in first.notifications[0][1] and "⏺" not in first.notifications[0][1]
    assert first.status == ["idle"]
    second = _run_stop_failure(monkeypatch, settings, AUTH_FAILURE)
    assert second.notifications == [] and second.events  # in the feed, no second push
    # a turn that answers clears it: the next expired login pushes again
    on_stop_claude.auth_alert_file(settings, "proj").unlink()
    assert _run_stop_failure(monkeypatch, settings, AUTH_FAILURE).notifications


def test_stop_failure_reports_a_connection_that_ran_out_of_retries(monkeypatch, settings):
    t = _run_stop_failure(monkeypatch, settings, CONNECTION_FAILURE)
    (level, message), = t.notifications
    assert level == "error" and "Restart" in message
    kind, payload = t.events[0]
    assert kind == "message" and "ECONNREFUSED" in payload["content"]
    assert not on_stop_claude.auth_alert_file(settings, "proj").exists()


# ── a stale "API Error" on screen never overrides a real answer ───────────────


def test_a_real_answer_wins_over_a_stale_pane_error(monkeypatch, settings, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(on_stop_claude, "limit_response_from_tmux", lambda s: "")
    monkeypatch.setattr(on_stop_claude, "api_error_from_tmux", lambda s: "API Error: Unable to connect to API (EPROTO)")
    monkeypatch.setattr(on_stop_claude, "response_from_transcript", lambda p: "Yes, TODO.md has 25 open items.")
    monkeypatch.setattr(on_stop_claude, "api_error_kind_from_transcript", lambda p: ("", ""))
    monkeypatch.setattr(on_stop_claude, "pane_raw", lambda s: "")
    response, _, _, api_error = on_stop_claude.run({"transcript_path": str(transcript)}, settings, "proj", "claude")
    assert response == "Yes, TODO.md has 25 open items." and api_error == ""
    message, level = on_stop.status_message(reason="", stop_type="", api_error_text=api_error,
                                            response_text=response, is_final_stop=False, agent="claude")
    assert level == "success" and message.startswith("Response ready")
