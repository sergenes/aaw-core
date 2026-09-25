"""Stop handler for Claude Code, Codex, and scoot. Called by the on_stop dispatcher.

The response comes from the agent's transcript when there is one (Claude Code),
from the stop payload (Codex, scoot), or as a last resort from the tmux pane.
Rate-limit and API-error banners are recognized so the phone gets an actionable
message instead of a raw error string.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aaw_core.config import Settings
from aaw_core.hooks.common import hook_log, tmux_session

LIMIT_KEYWORDS = ("out of extra usage", "usage limit", "rate limit", "out of usage")
API_ERROR_KEYWORDS = ("api error:", "sso session", "aws sso login", "invalid credentials")
_SPINNERS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷")


# ── tmux pane ───────────────────────────────────────────────────────────────


def pane_lines(session: str, scrollback: int = 500) -> list[str]:
    """The tmux pane, ANSI-stripped, as lines."""
    try:
        result = subprocess.run(["tmux", "capture-pane", "-t", f"={session}:", "-p", "-S", f"-{scrollback}"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    clean = re.sub(r"\x1B\[[0-9;]*[mGKHFABCDJK]", "", result.stdout)
    clean = re.sub(r"\x1B\(B", "", clean)
    return [ln.rstrip() for ln in clean.split("\n")]


def pane_raw(session: str) -> str:
    """The visible pane (no scrollback), for sentinel detection."""
    try:
        r = subprocess.run(["tmux", "capture-pane", "-t", f"={session}:", "-p"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


# ── transcript ──────────────────────────────────────────────────────────────


def _latest_real_entry(path_str: str):
    """The transcript's most recent entry with a role, skipping metadata-only lines."""
    path = Path(path_str)
    if not path_str or not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().split("\n")
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message", entry)
        if msg.get("role") in ("assistant", "user"):
            return entry
    return None


def response_from_transcript(path_str: str) -> str:
    """The assistant's text from the single most recent real transcript entry.

    Never falls back to older history: if the file ends mid-round (a tool result just
    landed, or the latest assistant entry is tool_use-only) the turn is not finished,
    so this returns "" rather than a stale earlier sentence.
    """
    entry = _latest_real_entry(path_str)
    if entry is None:
        return ""
    msg = entry.get("message", entry)
    if msg.get("role") == "user":
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = [b.get("text", "").strip() for b in content if isinstance(b, dict) and b.get("type") == "text"]
        texts = [t for t in texts if t]
        return texts[-1] if texts else ""
    return ""


def api_error_kind_from_transcript(path_str: str) -> tuple[str, str]:
    """(kind, text) if the latest transcript entry is one of Claude Code's own structured
    API-error messages (``isApiErrorMessage``), else ("", ""). kind is "auth", "limit",
    "prompt_too_long", or "connection"."""
    entry = _latest_real_entry(path_str)
    if entry is None:
        return "", ""
    msg = entry.get("message", entry)
    if msg.get("role") == "user" or not entry.get("isApiErrorMessage"):
        return "", ""
    content = msg.get("content", "")
    if isinstance(content, list):
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        text = str(content).strip()
    if not text:
        return "", ""
    low = text.lower()
    if "login expired" in low or "not logged in" in low:
        return "auth", text
    if "usage limit" in low or "usage credits" in low or "session limit" in low:
        return "limit", text
    if "prompt is too long" in low:
        return "prompt_too_long", text
    return "connection", text


def friendly_api_error_message(kind: str, text: str) -> str:
    """Plain language on what happened and what to do, with the original text kept verbatim."""
    if kind == "auth":
        return ("Claude Code needs you to sign back in: run /login in the terminal on your computer, "
                f"then resend your message.\n\n({text})")
    if kind == "prompt_too_long":
        return ("Claude's prompt got too long for this turn: try shortening your message or starting "
                f"a fresh session.\n\n({text})")
    if kind == "limit":
        return text  # already clear, and may carry a reset time the reminder feature parses
    return ("Connection to Claude's API was interrupted. This is usually a brief network problem, not "
            "a sign-in issue; Claude normally retries automatically. If this is the last message you "
            f"see, just resend your prompt.\n\n({text})")


# ── pane fallbacks ──────────────────────────────────────────────────────────


def looks_like_stray_line_dump(lines: list[str]) -> bool:
    """True if every line looks like `cat -n` file content (an indented line number, then
    text) rather than prose: the pane fallback can catch a transient Read-tool preview.
    A genuine numbered list starts at 1, so a block whose first number is not 1 is noise."""
    if not lines:
        return False
    nums = []
    for ln in lines:
        m = re.match(r"^\s{2,}(\d{1,5})(?:\s+\S.*)?$", ln)
        if not m:
            return False
        nums.append(int(m.group(1)))
    return nums[0] != 1


def response_from_tmux(session: str) -> str:
    """The last response from the pane, scanning bottom-up to the user's last prompt or the
    last tool call (the final prose always follows the last tool call)."""
    lines = pane_lines(session)
    if not lines:
        return ""
    response: list[str] = []
    for line in reversed(lines):
        s = line.strip()
        if re.match(r"^❯\s+[^/]", s) or s.startswith("⏺"):
            break
        if (not s or s == "❯"
                or all(c in "─│╭╮╰╯├┤┬┴┼▶◀● " for c in s)
                or s.startswith(("✓", "⎿", "↳", "esc ", "ctrl ", "?"))
                or s[0] in _SPINNERS
                or "running stop hook" in s.lower() or "running hook" in s.lower()
                or re.search(r"shift\+tab|accept edits|tab to cycle", s, re.IGNORECASE)
                or re.match(r"^(Read|Added|Updated|Wrote|Ran|Created|Deleted|Found|Listed|Searched|Replaced)\s+\d+\s+\w", s)):
            continue
        response.insert(0, line)
    if not response or looks_like_stray_line_dump(response):
        return ""
    return re.sub(r"^[⏺\s]+", "", "\n".join(response), flags=re.MULTILINE).strip()


def scan_pane_for_keywords(session: str, keywords: tuple[str, ...]) -> str:
    """The first of the last 50 pane lines containing a keyword, plus up to 4 lines after it."""
    lines = pane_lines(session, scrollback=50)
    for i, line in enumerate(lines):
        text = re.sub(r"^[⎿\s]+", "", line.strip()).strip()
        if any(kw in text.lower() for kw in keywords):
            out = [text]
            for j in range(i + 1, min(i + 5, len(lines))):
                nxt = re.sub(r"^[⎿\s]+", "", lines[j].strip()).strip()
                if nxt and not re.match(r"^❯", nxt):
                    out.append(nxt)
                else:
                    break
            return "\n".join(out)
    return ""


def limit_response_from_tmux(session: str) -> str:
    return scan_pane_for_keywords(session, LIMIT_KEYWORDS)


def api_error_from_tmux(session: str) -> str:
    return scan_pane_for_keywords(session, API_ERROR_KEYWORDS)


def parse_reset_info(text: str) -> tuple[int, str]:
    """Parse "resets 2pm (America/New_York)" from a rate-limit message into
    (unix timestamp, label), or (0, "")."""
    m = re.search(r"resets\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*[ap]m)(?:\s*\(([^)]+)\))?", text, re.IGNORECASE)
    if not m:
        return 0, ""
    time_str = m.group(1).strip()
    try:
        tz = ZoneInfo((m.group(2) or "UTC").strip())
    except Exception:  # noqa: BLE001 - ZoneInfo raises several types for an unknown zone
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    normalized = time_str.upper().replace(" ", "")
    parsed = None
    for fmt in ("%I%p", "%I:%M%p"):
        try:
            parsed = datetime.strptime(normalized, fmt).replace(tzinfo=tz)  # only hour/minute are used
            break
        except ValueError:
            continue
    if parsed is None:
        return 0, ""
    reset = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return int(reset.timestamp()), f"{time_str} {reset.strftime('%Z')}"


# ── entry point ─────────────────────────────────────────────────────────────


def run(payload: dict, settings: Settings, project: str, agent: str) -> tuple[str, str, bool, str]:
    """Returns (response_text, user_prompt, is_final_stop, api_error_text)."""
    session = tmux_session(project)
    transcript_path = payload.get("transcript_path", "")

    if agent in ("codex", "scoot"):
        # Both hand us last_assistant_message directly. scoot's transcript_path is its own
        # session JSON, not a Claude transcript, so never parse it as one.
        response = payload.get("last_assistant_message") or "" or response_from_tmux(session)
        is_final = not payload.get("last_assistant_message")  # only a true exit is final
        return response, "", is_final, ""

    limit_text = limit_response_from_tmux(session)
    api_error = api_error_from_tmux(session) if not limit_text else ""
    response = ""
    if transcript_path:
        # The transcript's final text can lag the Stop event on tool-heavy turns:
        # three checkpoints (now, +3s, +5s) instead of tight polling.
        start = time.monotonic()
        response = response_from_transcript(transcript_path)
        for checkpoint in (3.0, 5.0):
            if response:
                break
            remaining = checkpoint - (time.monotonic() - start)
            if remaining > 0:
                time.sleep(remaining)
            response = response_from_transcript(transcript_path)
        if not response:
            hook_log(settings, "on_stop", f"transcript empty after retries; falling back to tmux ({transcript_path})")

    if response:
        # A real transcript reply wins over any banner sitting in the pane's scrollback.
        # But the reply can itself be a structured API-error message: swap in an actionable one.
        kind, text = api_error_kind_from_transcript(transcript_path)
        if kind:
            response = friendly_api_error_message(kind, text)
            api_error = response
    elif limit_text:
        response = limit_text
    elif api_error:
        response = api_error
    else:
        response = response_from_tmux(session)
        hook_log(settings, "on_stop", f"tmux fallback used, first 200 chars: {response[:200]!r}")

    is_final = "--- session ended ---" in pane_raw(session)  # sentinel echoed by the start script
    return response, "", is_final, api_error
