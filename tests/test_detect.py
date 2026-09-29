"""The pane-scraping detectors, against synthetic panes shaped like the real widgets."""

from __future__ import annotations

import json

from aaw_core.host import detect

PERM_PANE = """\
⏺ Write(test999.md)
  ╭──────────────────────────────╮
  │ Do you want to create test999.md? │
  ╰──────────────────────────────╯
  ❯ 1. Yes
    2. Yes, and don't ask again for edits in this session
    3. No
"""

PLAN_MENU_PANE = """\
  Here is the plan:
  1. Refactor the auth module
  2. Add tests
  3. Update docs

  Ready to code?
  ❯ 1. Yes, and use auto mode
    2. Yes, manually approve edits
    3. Tell Claude what to change
"""

NUMBERED_LIST_PANE = """\
⏺ Three options:
  1. Yes we could do A
  2. No we should do B
❯ tell me more
"""

AQ_PANE = """\
  ☐ Color

  Pick your favorite
  color:

  ❯ 1. Red
       Red
    2. Blue                    ┌──────────────┐
       Blue                    │ preview text │
    3. Type something.         └──────────────┘
  ──────────────────────────────
    4. Chat about this

  Enter to select · ↑/↓ to navigate · Esc to cancel
"""

MULTI_PANE = """\
  ← ☐ Symptom ✔ Submit →

  Which apply?
  ❯ 1. [ ] Slow
    2. [x] Crashes
    3. Type something.
    4. Chat about this

  Enter to select · ↑/↓ to navigate · Esc to cancel
"""

CURSOR_SHELL_PANE = """\
Run this command?
Not in allowlist: cd, printf, python3
 -> Run (once) (y)
    Add Shell(...) to allowlist? (tab)
    Run Everything (shift+tab)
    Skip & tell the agent what to do instead (esc or n)
"""

CURSOR_FETCH_PANE = """\
Web Fetch: https://example.com/page
Allow this web fetch?
 -> Fetch (y)
    Run Everything (shift+tab)
    Skip (esc or n)
"""


def test_permission_prompt_with_tool_call_question():
    perm = detect.detect_permission_prompt(PERM_PANE)
    assert perm is not None
    assert perm["question"] == "Write(test999.md)"
    assert perm["options"] == ["Yes", "Yes, and don't ask again for edits in this session", "No"]
    assert perm["structure"] == "opts=3;nums=1,2,3"
    assert len(perm["hash"]) == 32
    assert detect.is_yes_no_shaped(perm["options"])


def test_plan_menu_is_detected_by_cursor_only_and_is_not_yes_no_shaped():
    perm = detect.detect_permission_prompt(PLAN_MENU_PANE)
    assert perm is not None  # the earlier "1. 2. 3." plan steps are a separate, ignored group
    assert perm["options"][0] == "Yes, and use auto mode"
    assert not detect.is_yes_no_shaped(perm["options"])  # so auto-approve must not send "1"


def test_numbered_list_followed_by_input_is_not_a_prompt():
    assert detect.detect_permission_prompt(NUMBERED_LIST_PANE) is None
    assert detect.detect_permission_prompt("just prose\n") is None


def test_ask_user_question_single_select_with_preview_panel():
    aq = detect.detect_ask_user_question(AQ_PANE)
    assert aq is not None
    assert aq["question"] == "Pick your favorite color:"  # wrapped question is re-joined
    assert aq["options"] == ["Red", "Blue"]  # preview panel stripped, meta rows dropped
    assert detect.detect_multiselect_prompt(AQ_PANE) is None  # no Submit tab
    assert detect.detect_ask_user_question("no footer here") is None


def test_multiselect_is_left_to_the_chat_about_this_fallback():
    assert detect.detect_ask_user_question(MULTI_PANE) is None  # checkbox rows: no phone UI
    ms = detect.detect_multiselect_prompt(MULTI_PANE)
    assert ms is not None
    assert ms["label"] == "Symptom" and ms["down_presses"] == 3


def test_cursor_native_prompt_variants_and_session_keys():
    shell = detect.detect_cursor_native_prompt(CURSOR_SHELL_PANE)
    assert shell is not None
    assert shell["question"].startswith("Run this command? Not in allowlist")
    assert shell["options"] == ["Yes", "Yes for this session", "No"]
    assert shell["session_key"] == "Tab"  # "(tab)" offered
    fetch = detect.detect_cursor_native_prompt(CURSOR_FETCH_PANE)
    assert fetch is not None
    assert "https://example.com/page" in fetch["question"]
    assert fetch["session_key"] == "BTab"  # only "(shift+tab)" offered
    assert detect.detect_cursor_native_prompt("Run this command?\n(y) only, no esc option") is None
    assert detect.CURSOR_NATIVE_ANSWER_KEYS == {"Yes": "y", "No": "n"}


def test_idle_detectors():
    assert detect.detect_idle_prompt("some output\n────────\n❯ \n")
    assert not detect.detect_idle_prompt("⠋ Thinking\n❯ \n")  # spinner: busy
    assert not detect.detect_idle_prompt("no prompt at all")


def test_idle_prompt_claude_2x_turn_is_busy():
    # Claude Code 2.x mid-turn (real 2.1.284 capture): new spinner glyphs, the ❯ composer
    # visible, and "esc to interrupt" in the shortcut row. This pane once read as idle and
    # killed the phone's working indicator seconds into the turn.
    mid_turn = (
        "❯ Please run the shell command sleep 45 and then write a summary.\n"
        "· Pouncing… (2s · thinking)\n"
        "────────\n"
        "❯ \n"
        "────────\n"
        "  ⏸ manual mode on · esc to interrupt · ← 1 agent\n")
    assert not detect.detect_idle_prompt(mid_turn)
    # The real idle prompt: the shortcut row shows "? for shortcuts" instead, and the
    # finished-turn line ("✻ ... · done ...") must not read as a spinner.
    idle = (
        "✻ Sautéed for 4m 26s · done Sunday 10:34 PM\n"
        "────────\n"
        "❯ \n"
        "────────\n"
        "  ⏸ manual mode on · ? for shortcuts · ← 1 agent\n")
    assert detect.detect_idle_prompt(idle)
    assert detect.detect_gemini_idle(" >   Type your message or @path/to/file")
    assert not detect.detect_gemini_idle("✦ generating")
    assert detect.detect_cursor_idle("...\n-> Add a follow-up")
    assert not detect.detect_cursor_idle("still working")


def test_cursor_error_prefers_chat_message():
    pane = ("Error: usage limit\n"
            "chatMessage: *You have hit your usage limit.\n"
            "It resets on Monday.*\n"
            "spendLimits: ...\n")
    assert detect.detect_cursor_error(pane) == "You have hit your usage limit. It resets on Monday."
    assert detect.detect_cursor_error("Error: something terse\n") == "something terse"
    assert detect.detect_cursor_error("all good") == ""


def test_claude_retry_banner():
    retry = detect.detect_claude_retry("✻ Connection dropped (ECONNRESET) · Retrying in 10s · attempt 6/10")
    assert retry == {"message": "Connection dropped (ECONNRESET)", "attempt": 6, "total": 10}
    assert detect.detect_claude_retry("Retrying later") is None
    assert detect.CLAUDE_RETRY_ALERT_ATTEMPT == 3


def test_gemini_response_extraction():
    pane = ("✦ Here is the answer.\n"
            "  It has two lines.\n"
            "─────────────────────\n"
            " >   Type your message or @path/to/file\n")
    assert detect.extract_gemini_response(pane) == "Here is the answer.\nIt has two lines."
    assert detect.extract_gemini_response("no diamond") == ""


def test_pane_input_is_empty_distinguishes_input_box_from_dialogs():
    assert detect.pane_input_is_empty("output\n────────────\n❯ \n") is True
    assert detect.pane_input_is_empty("output\n────────────\n❯ half typed\n") is False
    assert detect.pane_input_is_empty(AQ_PANE) is None  # the dialog's "❯ 1. Red" is not an input box
    assert detect.pane_input_is_empty("nothing") is None


def test_last_logged_assistant_message(tmp_path):
    log = tmp_path / "p.jsonl"
    assert detect.last_logged_assistant_message(log) == ""
    log.write_text("\n".join([
        json.dumps({"type": "message", "role": "user", "content": "q"}),
        json.dumps({"type": "message", "role": "assistant", "content": "first"}),
        "broken",
        json.dumps({"type": "message", "role": "assistant", "content": "last"}),
    ]))
    assert detect.last_logged_assistant_message(log) == "last"


def test_dialog_is_open():
    assert detect.dialog_is_open(PERM_PANE, "claude")
    assert detect.dialog_is_open(AQ_PANE, "claude")
    assert detect.dialog_is_open(MULTI_PANE, "claude")
    assert detect.dialog_is_open(CURSOR_SHELL_PANE, "cursor")
    assert not detect.dialog_is_open(CURSOR_SHELL_PANE, "claude")  # the Cursor widget is cursor-only
    assert not detect.dialog_is_open("output\n────\n❯ \n", "claude")


def test_has_claude_history_encodes_the_path_like_claude(tmp_path, monkeypatch):
    monkeypatch.setattr("aaw_core.host.detect.Path.home", lambda: tmp_path)
    project = tmp_path / "ai_projects" / "x.y"
    hist = tmp_path / ".claude" / "projects" / f"-{str(project)[1:].replace('/', '-').replace('_', '-').replace('.', '-')}"
    assert not detect.has_claude_history(project)
    hist.mkdir(parents=True)
    assert not detect.has_claude_history(project)  # a folder with no transcript does not count
    (hist / "s.jsonl").write_text("{}")
    assert detect.has_claude_history(project)


def test_strip_ansi():
    assert detect.strip_ansi("\x1b[31mred\x1b[0m \x1b[?25hcursor") == "red cursor"


TRUST_PANE = """
 Accessing workspace:
 /home/scoot/dev/aaw-test
 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""


def test_claude_trust_dialog_is_a_dialog_not_an_idle_prompt():
    d = detect.detect_claude_trust_dialog(TRUST_PANE)
    assert d["folder"] == "/home/scoot/dev/aaw-test"
    assert d["options"] == ["Yes, I trust this folder", "No, exit"]
    assert d["question"].endswith(": /home/scoot/dev/aaw-test")
    assert detect.detect_idle_prompt(TRUST_PANE) is False
    assert detect.detect_permission_prompt(TRUST_PANE) is None
    assert detect.dialog_is_open(TRUST_PANE, "claude") is True
    assert detect.dialog_is_open(TRUST_PANE, "codex") is False
    assert detect.detect_claude_trust_dialog("❯ \n") is None
    assert detect.is_yes_no_shaped(d["options"])
