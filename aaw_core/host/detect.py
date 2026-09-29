"""Pane-scraping detectors: pure functions of tmux pane text.

Everything the daemon learns by looking at the agent's terminal lives here, so it
can be unit-tested against captured panes without tmux. Fragile by nature: each
detector matches a specific widget's shape and needs re-verification when an agent
changes its rendering.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SPINNERS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷")

# Keystroke per answer for Cursor's native widget (single key, no Enter). "Yes for this
# session" is resolved per prompt instance (see detect_cursor_native_prompt).
CURSOR_NATIVE_ANSWER_KEYS = {"Yes": "y", "No": "n"}

# Failed attempts before a Claude retry episode is worth telling the phone about.
CLAUDE_RETRY_ALERT_ATTEMPT = 3


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences, including DEC private modes."""
    return _ANSI_RE.sub("", text)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ── Claude Code permission prompt (numbered Yes / No menu) ──────────────────


def detect_permission_prompt(content: str) -> dict | None:
    """Claude Code's numbered permission menu ("1. Yes / 2. Yes, and don't ask again /
    3. No"), which waits for a single keypress. Returns {question, options, hash,
    structure} or None."""
    lines = content.split("\n")
    opt_re = re.compile(r"^\s*(❯\s*)?(\d+)[.)]\s+(.+)")
    found = []  # (line index, number, text, has cursor)
    for i, line in enumerate(lines):
        m = opt_re.match(line)
        if m:
            found.append((i, int(m.group(2)), m.group(3).strip(), bool(m.group(1))))
    if len(found) < 2:
        return None

    # A plan's own "1. / 2. / 3." steps can sit just above a real menu. A live option
    # block is tightly packed, so group by proximity and keep only the last group.
    groups = [[found[0]]]
    for f in found[1:]:
        if f[0] - groups[-1][-1][0] <= 2:
            groups[-1].append(f)
        else:
            groups.append([f])
    found = groups[-1]
    if len(found) < 2:
        return None

    nums = [f[1] for f in found]
    if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
        return None
    first_idx, last_idx = found[0][0], found[-1][0]
    if last_idx - first_idx > 15:
        return None
    # A ❯ with content right after the options means Claude printed a numbered list.
    for line in lines[last_idx + 1: last_idx + 4]:
        if re.match(r"^\s*❯\s+\S", line):
            return None

    option_texts = [f[2] for f in found]
    has_yes = any(re.match(r"yes\b", t, re.IGNORECASE) for t in option_texts)
    has_no = any(re.match(r"no\b", t, re.IGNORECASE) for t in option_texts)
    # The ❯ cursor never appears on a line Claude prints as prose, so it alone marks a
    # live menu even when the options are not worded Yes/No (the plan-approval menu).
    has_cursor = any(f[3] for f in found)
    if not (has_yes and has_no) and not has_cursor:
        return None

    box = set("─│╭╮╰╯├┤┬┴┼ ")
    skip_starts = ("✓", "⎿", "↳", "❯", "?", "⠋", "⠙", "⠹")
    question = ""
    for line in reversed(lines[max(0, first_idx - 20): first_idx]):
        s = line.strip()
        if s.startswith("⏺"):  # the tool-call line just before the prompt
            question = re.sub(r"^⏺\s*", "", s).strip()
            break
    if not question:
        question_lines = []
        for line in lines[max(0, first_idx - 10): first_idx]:
            s = re.sub(r"^[│┃]\s*", "", line.strip()).strip()
            if not s or all(c in box for c in s) or s[0] in skip_starts:
                continue
            if re.match(r"^[-\u2013\u2014_.=•\s]+$", s) or s == "..." or (len(s) <= 3 and not s[0].isalnum()):
                continue
            question_lines.append(s)
        question = " ".join(question_lines[-2:]) if question_lines else "Claude needs permission"

    block = "\n".join(lines[max(0, first_idx - 3): last_idx + 2])
    return {
        "question": question,
        "options": option_texts,
        "hash": _md5(block),
        "structure": f"opts={len(found)};nums={','.join(str(n) for n in nums)}",
    }


def is_yes_no_shaped(options: list) -> bool:
    """A genuine Yes / Yes-for-session / No prompt: 2-3 options with a Yes and a No.
    Gates auto-approve, since a cursor-only match (the plan-approval menu, a custom
    3-option question) must never get key "1" sent blindly."""
    if len(options) not in (2, 3):
        return False
    has_yes = any(re.match(r"yes\b", o, re.IGNORECASE) for o in options)
    has_no = any(re.match(r"no\b", o, re.IGNORECASE) for o in options)
    return has_yes and has_no


# ── Cursor's native confirmation widget ────────────────────────────────────


def detect_cursor_native_prompt(content: str) -> dict | None:
    """Cursor CLI's native y / tab / esc confirmation for any tool type, matched by
    shape: a line ending in "?" with "(y)" and some "(esc...)" variant within the next
    few lines. Returns {question, options, hash, session_key} or None."""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.endswith("?"):
            continue
        window = "\n".join(lines[i: i + 8])
        if "(y)" not in window or not re.search(r"\(esc[^)]*\)", window):
            continue
        detail: list[str] = []
        for prev in reversed(lines[max(0, i - 4): i]):
            p = prev.strip()
            if not p:
                if detail:
                    break
                continue
            detail.insert(0, p)
        if not detail:
            for nxt in lines[i + 1: i + 4]:
                p = nxt.strip()
                if not p:
                    if detail:
                        break
                    continue
                if "(y)" in p or p.startswith(("-", "→")):
                    break
                detail.append(p)
        question = " ".join([s] + detail) if detail else s
        block = "\n".join(lines[max(0, i - 4): i + 8])
        # Which key grants broader approval varies per instance: "(tab)" adds these
        # commands to the allowlist, "(shift+tab)" is Run Everything; not both.
        if "(tab)" in window:
            session_key = "Tab"
        elif "(shift+tab)" in window:
            session_key = "BTab"  # tmux's name for Shift+Tab
        else:
            session_key = "y"
        return {"question": question[:200], "options": ["Yes", "Yes for this session", "No"],
                "hash": _md5(block), "session_key": session_key}
    return None


# ── Claude Code AskUserQuestion widgets ────────────────────────────────────


def _footer_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if "Enter to select" in line and "Esc to cancel" in line:
            return i
    return None


def detect_ask_user_question(content: str) -> dict | None:
    """Claude Code's single-select AskUserQuestion picker (arrow keys + Enter).
    Multi-select variants ([ ] checkboxes) are deliberately not detected here.
    Returns {question, options, hash} or None."""
    lines = content.split("\n")
    footer_idx = _footer_index(lines)
    if footer_idx is None:
        return None
    opt_re = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.+)$")
    # A preview panel drawn with box characters to the right of an option would
    # otherwise be captured as part of its text.
    preview_panel_re = re.compile(r"\s{2,}[┌┐└┘│─┬┴├┤┼╭╮╰╯].*$")
    found = []
    for i, line in enumerate(lines[:footer_idx]):
        m = opt_re.match(line)
        if m:
            found.append((i, int(m.group(1)), preview_panel_re.sub("", m.group(2)).strip()))
    if len(found) < 2:
        return None
    nums = [f[1] for f in found]
    if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
        return None
    option_texts = [f[2] for f in found]
    if any(re.match(r"^\[[ xX]\]\s", t) for t in option_texts):
        return None  # multi-select: no phone UI for it
    real = []
    for text in option_texts:  # drop the trailing meta rows
        if re.match(r"^type something\.?$", text, re.IGNORECASE) or re.match(r"^chat about this$", text, re.IGNORECASE):
            break
        real.append(text)
    if not real:
        return None
    first_idx = found[0][0]
    question_lines: list[str] = []  # contiguous lines above the options; the question may wrap
    for j in range(first_idx - 1, max(-1, first_idx - 8), -1):
        s = lines[j].strip()
        if not s:
            if question_lines:
                break
            continue
        if any(ch in s for ch in "☐✔←→") or "Submit" in s:
            break
        question_lines.insert(0, s)
    question = " ".join(question_lines) or "Claude is asking a question"
    block = "\n".join(lines[max(0, first_idx - 2): footer_idx])
    return {"question": question, "options": real, "hash": _md5(block)}


def detect_multiselect_prompt(content: str) -> dict | None:
    """Claude Code's multi-select AskUserQuestion (checkbox rows + a "Submit" tab).
    The phone cannot answer it, so the daemon selects the trailing "Chat about this"
    row instead. Returns {label, hash, down_presses} or None."""
    lines = content.split("\n")
    footer_idx = _footer_index(lines)
    if footer_idx is None:
        return None
    glyphs = "☐✔□✓"  # varies by Claude Code version / terminal font
    header_idx = None
    label = "a question"
    for i, line in enumerate(lines[:footer_idx]):
        s = line.strip()
        if "Submit" in s and (any(ch in s for ch in glyphs) or "←" in s or "→" in s):
            header_idx = i
            m = re.search(rf"[{glyphs}]\s*([A-Za-z][\w \-]*?)\s+[{glyphs}]\s*Submit", s)
            if m:
                label = m.group(1).strip()
            break
    if header_idx is None:
        return None
    opt_re = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.+)$")
    rows = []
    for line in lines[header_idx:footer_idx]:
        m = opt_re.match(line)
        if m:
            rows.append((int(m.group(1)), m.group(2).strip()))
    if len(rows) < 2:
        return None
    nums = [r[0] for r in rows]
    if nums[0] != 1 or nums != list(range(1, len(nums) + 1)):
        return None
    if not re.match(r"^chat about this$", rows[-1][1], re.IGNORECASE):
        return None
    block = "\n".join(lines[header_idx:footer_idx])
    return {"label": label, "hash": _md5(block), "down_presses": len(rows) - 1}


# ── idle / error / retry states ────────────────────────────────────────────


TRUST_YES = "Yes, I trust this folder"
TRUST_NO = "No, exit"


def detect_claude_trust_dialog(pane: str) -> dict | None:
    """Claude Code's dialog on the first open of a folder ("Is this a project you created
    or one you trust?"): arrow keys move, Enter confirms, "No, exit" is preselected. A
    headless start on a fresh folder sits here until someone answers. Returns
    {question, options, folder, hash} or None."""
    if TRUST_YES not in pane or "Enter to confirm" not in pane:
        return None
    lines = [ln.strip() for ln in pane.split("\n")]
    folder = ""
    for i, ln in enumerate(lines):
        if ln.startswith("Accessing workspace:") and i + 1 < len(lines):
            folder = lines[i + 1]
            break
    where = f" {folder}" if folder else ""
    question = f"Claude Code asks whether to trust this folder before it reads, edits and runs files there:{where}"
    return {"question": question, "options": [TRUST_YES, TRUST_NO], "folder": folder,
            "hash": hashlib.sha1(f"trust:{folder}".encode()).hexdigest()}


def detect_idle_prompt(pane: str) -> bool:
    """Claude is at the ❯ input prompt and not processing (no spinner anywhere). The trust
    dialog's "❯ No, exit" is a cursor on an option, not the input prompt."""
    # Claude Code 2.x: the ❯ composer stays visible for the whole turn and the spinner
    # glyphs changed, so the shortcut row is the reliable signal. It reads "esc to
    # interrupt" during the turn and "? for shortcuts" at the real idle prompt
    # (confirmed on 2.1.284 with pane captures).
    if "esc to interrupt" in pane:
        return False
    if any(c in SPINNERS for c in pane) or TRUST_YES in pane:
        return False
    tail = [ln.strip() for ln in pane.split("\n") if ln.strip()][-6:]
    return any(ln.startswith("❯") for ln in tail)


def detect_gemini_idle(pane: str) -> bool:
    """Gemini CLI shows "Type your message or @path/to/file" only when idle."""
    return "Type your message" in pane


def detect_cursor_idle(pane: str) -> bool:
    """Cursor CLI prints "Add a follow-up" at the bottom once a turn finishes."""
    return "Add a follow-up" in pane


def detect_cursor_error(pane: str) -> str:
    """Cursor CLI's own error block (e.g. its usage limit), which fires no hook and
    shows no idle marker. Prefers the friendlier "chatMessage:" text. "" if none."""
    m = re.search(r"^\s*Error:\s*(.+)$", pane, re.MULTILINE)
    if not m:
        return ""
    cm = re.search(r"chatMessage:\s*(.+?)(?=\n\s*spendLimits|\n\s*\n|\Z)", pane, re.DOTALL)
    if cm:
        text = re.sub(r"^\*(.+)\*$", r"\1", cm.group(1).strip(), flags=re.DOTALL).strip()
        return re.sub(r"\s*\n\s*", " ", text)  # un-wrap the pane's line breaks
    return m.group(1).strip()


def detect_claude_retry(pane: str) -> dict | None:
    """Claude Code's in-progress API retry banner ("<error> · Retrying in 10s · attempt
    6/10"). The turn never ends while retrying, so no Stop hook fires; this is the only
    signal. Matches the structure, not a specific error. Returns {message, attempt, total}."""
    m = re.search(r"(?P<desc>[^\n·]+?)\s*·\s*Retrying in\s+\d+\s*s\s*·\s*attempt\s+(?P<attempt>\d+)\s*/\s*(?P<total>\d+)",
                  pane)
    if not m:
        return None
    desc = re.sub(r"^[^\w(]+", "", m.group("desc").strip()).strip()  # drop the animating spinner glyph
    if not desc:
        return None
    return {"message": desc, "attempt": int(m.group("attempt")), "total": int(m.group("total"))}


# ── responses from panes and transcripts ───────────────────────────────────


def extract_gemini_response(pane: str) -> str:
    """The last Gemini CLI response: lines from the last "✦" up to the input border."""
    lines = pane.split("\n")
    last_diamond = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("✦"):
            last_diamond = i
    if last_diamond == -1:
        return ""
    stoppers = ("▄", "▀", "─", "ℹ", "Shift+Tab", "? for shortcuts", "workspace (", "branch", "sandbox")
    out = []
    for line in lines[last_diamond:]:
        s = line.strip()
        if any(s.startswith(x) or x in s for x in stoppers):
            break
        if s:
            out.append(s.removeprefix("✦ "))
    return "\n".join(out).strip()


def cursor_latest_transcript(project_dir: str) -> str | None:
    """The most recently modified Cursor transcript for this project directory. Cursor
    encodes the path like Claude Code (non-alphanumerics to "-") but drops the leading
    dash: "/Users/x/y" becomes "Users-x-y"."""
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", project_dir).lstrip("-")
    transcripts_dir = Path.home() / ".cursor" / "projects" / encoded / "agent-transcripts"
    try:
        candidates = list(transcripts_dir.glob("*/*.jsonl"))
    except OSError:
        return None
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def last_logged_assistant_message(log_path: Path) -> str:
    """The most recent assistant message already in the local session log, so an idle
    fallback never double-writes what a hook already captured."""
    if not log_path.exists():
        return ""
    last = ""
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "message" and entry.get("role") == "assistant":
                last = entry.get("content", "")
    except OSError:
        return ""
    return last


def pane_input_is_empty(pane: str) -> bool | None:
    """Claude Code only: True when the input box (the ❯ line directly under a full-width
    separator) is empty, False when it holds text, None when no input box is visible.
    The separator check keeps an AskUserQuestion's "❯ 1. Red" from reading as typed text."""
    lines = pane.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped.startswith("❯"):
            continue
        prev = ""
        for j in range(i - 1, -1, -1):
            if lines[j].strip():
                prev = lines[j].strip()
                break
        if prev and set(prev) <= {"─"}:
            return stripped[1:].strip() == ""
    return None


def has_claude_history(project_dir: Path) -> bool:
    """Whether Claude Code has a transcript for this folder (so --continue works). Claude
    replaces every non-alphanumeric character with "-"; a folder with no *.jsonl does not count."""
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(project_dir))
    hist = Path.home() / ".claude" / "projects" / encoded
    return hist.is_dir() and any(hist.glob("*.jsonl"))


def has_scoot_history(project_dir: Path) -> bool:
    """Whether scoot has a saved session for this workspace root (so --continue resumes it).
    scoot keeps one JSON per session under ~/.local/state/scoot/sessions (SCOOT_STATE_DIR overrides)."""
    state = Path(os.environ.get("SCOOT_STATE_DIR") or Path.home() / ".local/state/scoot")
    for f in (state / "sessions").glob("*.json"):
        try:
            if json.loads(f.read_text()).get("root") == str(project_dir):
                return True
        except (OSError, ValueError):
            continue
    return False


def dialog_is_open(pane: str, agent: str) -> bool:
    """A prompt that owns the keyboard is on screen, so typed text would go into it."""
    return bool(
        detect_permission_prompt(pane)
        or detect_ask_user_question(pane)
        or detect_multiselect_prompt(pane)
        or (agent == "claude" and detect_claude_trust_dialog(pane))
        or (agent == "cursor" and detect_cursor_native_prompt(pane))
    )
