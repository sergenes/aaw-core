"""A prompt the agent refuses without starting a turn reaches the phone as an error."""

from __future__ import annotations

import time

from aaw_core import daemon
from aaw_core.hooks.on_stop_claude import api_error_kind_from_text
from aaw_core.host import detect

# The incident this feature comes from, verbatim (2026-09-28).
REFUSAL_PANE = """\
✻ Sautéed for 4m 26s · done Sunday 10:34 PM

❯ Hey
  ⎿  You're out of usage credits. Run /usage-credits to keep using Fable 5.1 or /model to switch
     models.

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏸ manual mode on · ? for shortcuts · ← 1 agent
"""


def test_the_incident_pane_yields_the_error_text():
    got = detect.detect_refused_prompt(REFUSAL_PANE, "Hey")
    assert got == ("You're out of usage credits. Run /usage-credits to keep using Fable 5.1"
                   " or /model to switch models.")


def test_wrapped_prompt_echo_still_matches():
    pane = ("❯ Please summarize the whole repository structure and then tell me which\n"
            "  modules changed most recently in detail\n"
            "  ⎿  Login expired. Run /login to sign back in.\n"
            "\n❯\n")
    got = detect.detect_refused_prompt(pane, "Please summarize the whole repository structure and then tell me which modules changed most recently in detail")
    assert got == "Login expired. Run /login to sign back in."


def test_an_answered_prompt_is_not_a_refusal():
    pane = ("❯ Hey\n"
            "⏺ Hello! How can I help?\n"
            "  ⎿  some tool output that also uses the glyph\n"
            "❯\n")
    assert detect.detect_refused_prompt(pane, "Hey") is None


def test_a_running_turn_and_a_missing_echo_are_not_refusals():
    assert detect.detect_refused_prompt("❯ Hey\n  ⎿  error\nesc to interrupt", "Hey") is None
    assert detect.detect_refused_prompt("❯ Other prompt\n  ⎿  error\n", "Hey") is None
    assert detect.detect_refused_prompt("❯ Hey\n────\n❯\n", "Hey") is None  # just the composer


def test_error_families():
    assert api_error_kind_from_text("You're out of usage credits. Run /usage-credits") == "limit"
    assert api_error_kind_from_text("Login expired. Run /login") == "auth"
    assert api_error_kind_from_text("Prompt is too long") == "prompt_too_long"
    assert api_error_kind_from_text("Something else entirely") == ""


class _Transport:
    def __init__(self):
        self.notifications: list[tuple[str, str]] = []

    def write_notification(self, message, level):
        self.notifications.append((level, message))


def test_watch_reports_once_and_clears():
    t = _Transport()
    watch = {"text": "Hey", "ts": time.monotonic()}
    daemon.watch_for_refusal(t, "claude", REFUSAL_PANE, watch)
    assert watch == {}
    assert len(t.notifications) == 1
    level, message = t.notifications[0]
    assert level == "error" and "/usage-credits" in message
    daemon.watch_for_refusal(t, "claude", REFUSAL_PANE, watch)  # cleared: nothing more
    assert len(t.notifications) == 1


def test_watch_clears_on_turn_start_and_expiry():
    t = _Transport()
    watch = {"text": "Hey", "ts": time.monotonic()}
    daemon.watch_for_refusal(t, "claude", "✢ Thinking\n❯ \nesc to interrupt", watch)
    assert watch == {} and t.notifications == []
    watch = {"text": "Hey", "ts": time.monotonic() - 999}
    daemon.watch_for_refusal(t, "claude", REFUSAL_PANE, watch)
    assert watch == {} and t.notifications == []
    watch = {"text": "Hey", "ts": time.monotonic()}
    daemon.watch_for_refusal(t, "codex", REFUSAL_PANE, watch)  # Claude-shaped only, for now
    assert watch and t.notifications == []
