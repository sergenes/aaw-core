"""Stop handler for Grok Composer. Called by the on_stop dispatcher, never directly.

Grok fires several Stop events per turn (one per tool step plus one at turn end)
and a separate session_end event. The handler deduplicates by hashing the
captured response and skipping the same content within 60 seconds.

Primary source: ~/.grok/sessions/{cwd}/{session_id}/chat_history.jsonl, which Grok
writes before firing the hook, so the full text is available without pane
truncation (Grok Composer is an alternate-screen TUI: tmux capture-pane only sees
the viewport). Fallback: the tmux pane.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

from aaw_core.config import Settings
from aaw_core.hooks.common import hook_log, tmux_session

# Block Elements U+2580..U+259F: Grok's streaming cursor characters.
# Deliberately excludes the step indicator (U+25C6).
_GROK_BLOCK = re.compile(r"[▀-▟]")
_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷")
_BOX_CHARS = "─│╭╮╰╯├┤┬┴┼▶◀● "


def grok_clean(s: str) -> str:
    return _GROK_BLOCK.sub("", s).strip()


def is_grok_diamond(stripped: str, cleaned: str) -> bool:
    """True for a step/hook marker line. Grok prefixes the marker with a streaming
    cursor character whose code point varies, so allow any non-word prefix."""
    return stripped.startswith("◆") or cleaned.startswith("◆") or bool(re.match(r"^[^\w]*◆", stripped))


def is_chrome(c: str) -> bool:
    """True if the cleaned line is pure UI chrome."""
    if not c:
        return True
    if all(ch in _BOX_CHARS + "\t" for ch in c):
        return True
    if c.startswith(("\u2014", "\u2013", "✓", "⎿", "↳")):  # em dash / en dash: Grok's own separator lines
        return True
    if c.startswith("~/") or bool(re.match(r"^\d+K\s*/\s*\d+K", c)):  # header bar: path, context usage
        return True
    lc = c.lower()
    return (
        "always-approve" in lc or "grok build" in lc or "grok composer" in lc or "turn completed" in lc
        or bool(re.search(r"\bshift\+tab\b", lc)) or bool(re.search(r"\bctrl\+[a-z\d]\b", lc))
        or bool(re.search(r"\bspace:prompt\b", lc))
    )


def response_from_grok_pane(lines: list[str]) -> str:
    """Extract the response from ANSI-stripped pane lines.

    Primary path: the last "◆ user_prompt_submit" marker is visible; collect everything
    below it up to the spinner or the input box, skipping other markers and chrome.
    Fallback (the marker scrolled off): scan forward, resetting the collected text at
    confirmed turn boundaries (a new user_prompt_submit, or the input box), and treating
    a "◆ stop" as only a *pending* boundary that content afterwards cancels (an
    intermediate step stop within the same turn).
    """

    def is_box_chrome(stripped: str, c: str) -> bool:
        return any(ch in "╭╰" for ch in stripped) and all(ch in _BOX_CHARS for ch in c)

    ups_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if is_grok_diamond(stripped, grok_clean(stripped)) and "user_prompt_submit" in stripped:
            ups_idx = i

    collected: list[str] = []
    if ups_idx is not None:
        for line in lines[ups_idx + 1:]:
            stripped = line.strip()
            c = grok_clean(stripped)
            if stripped and stripped[0] in _SPINNER_CHARS:
                break
            if is_box_chrome(stripped, c):
                break
            if is_grok_diamond(stripped, c):
                if "user_prompt_submit" in stripped.lower():
                    break  # a new turn started
                continue  # ◆ stop / session_end / Thought / tool_use: not a boundary
            if is_chrome(c):
                continue
            collected.append(line)
    else:
        pending_reset = False
        for line in lines:
            stripped = line.strip()
            c = grok_clean(stripped)
            if stripped and stripped[0] in _SPINNER_CHARS:
                break
            if is_box_chrome(stripped, c):
                collected, pending_reset = [], False
                continue
            if is_grok_diamond(stripped, c):
                sl = stripped.lower()
                if "user_prompt_submit" in sl:
                    collected, pending_reset = [], False
                elif "stop" in sl or "session_end" in sl:
                    pending_reset = True
                continue
            if is_chrome(c):
                continue
            if pending_reset:
                pending_reset = False  # content after a stop: it was an intermediate step
            collected.append(line)

    if not collected:
        return ""
    text = _GROK_BLOCK.sub("", "\n".join(collected))
    text = re.sub(r"\s+\d{1,2}:\d{2}\s*[AP]M\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)  # inline timestamps
    return "\n".join(ln for ln in text.split("\n") if ln.strip()).strip()


def response_from_chat_history(payload: dict, settings: Settings) -> str:
    """The completed assistant response from Grok's own chat_history.jsonl."""
    session_id = os.environ.get("GROK_SESSION_ID") or payload.get("sessionId", "")
    workspace = os.environ.get("GROK_WORKSPACE_ROOT") or payload.get("workspaceRoot", "") or payload.get("cwd", "")
    if not session_id or not workspace:
        return ""
    encoded_cwd = urllib.parse.quote(workspace.rstrip("/"), safe="")
    chat_path = Path.home() / ".grok" / "sessions" / encoded_cwd / session_id / "chat_history.jsonl"
    if not chat_path.exists():
        hook_log(settings, "on_stop", f"grok chat_history not found: {chat_path}")
        return ""
    last_text = ""
    try:
        for line in chat_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("type") != "assistant":
                continue
            c = msg.get("content", "")
            if isinstance(c, str):
                text = c.strip()
            elif isinstance(c, list):
                text = "".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text").strip()
            else:
                text = ""
            if text:
                last_text = text
    except (OSError, ValueError) as e:
        hook_log(settings, "on_stop", f"grok chat_history read error: {e}")
        return ""
    return last_text


def capture_response(session: str) -> str:
    """Fallback: the response from the tmux pane."""
    try:
        result = subprocess.run(["tmux", "capture-pane", "-t", f"={session}:", "-p", "-S", "-3000"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    clean = re.sub(r"\x1B\[[0-9;]*[mGKHFABCDJK]", "", result.stdout)
    clean = re.sub(r"\x1B\(B", "", clean)
    return response_from_grok_pane([ln.rstrip() for ln in clean.split("\n")])


def run(payload: dict, settings: Settings, project: str) -> tuple[str, str, bool, str]:
    """Returns (response_text, user_prompt, is_final_stop, api_error_text)."""
    hook_event = os.environ.get("GROK_HOOK_EVENT", "")
    hook_log(settings, "on_stop", f"grok stop: hook_event={hook_event!r} reason={payload.get('reason', '')!r}")
    if hook_event == "session_end":
        return "", "", True, ""

    raw = response_from_chat_history(payload, settings)
    source = "chat_history"
    if not raw:
        raw = capture_response(tmux_session(project))
        source = "pane_capture"
    if not raw:
        return "", "", False, ""

    digest = hashlib.md5(raw.encode()).hexdigest()[:8]
    dedup_file = settings.state_dir / f"grok_last_{project or 'default'}"
    is_dup = False
    try:
        if dedup_file.exists():
            parts = dedup_file.read_text().split("|")
            if len(parts) == 2 and parts[0] == digest and time.time() - float(parts[1]) < 60:
                is_dup = True
        if not is_dup:
            dedup_file.parent.mkdir(parents=True, exist_ok=True)
            dedup_file.write_text(f"{digest}|{time.time()}")
    except (OSError, ValueError):
        pass
    hook_log(settings, "on_stop", f"grok response source={source} hash={digest} is_dup={is_dup} len={len(raw)}")
    return ("" if is_dup else raw), "", False, ""
