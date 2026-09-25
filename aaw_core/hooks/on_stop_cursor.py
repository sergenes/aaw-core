"""Stop handler for Cursor. Called by the on_stop dispatcher, never directly.

Cursor writes a structured JSONL transcript per session (role + typed content
blocks, the same shape as Claude Code's) and names it in the stop payload's
``transcript_path``. That is a clean source, not a pane scrape.
"""

from __future__ import annotations

import json
from pathlib import Path


def response_from_transcript(transcript_path: str) -> str:
    """The last assistant text reply: only type:"text" blocks of the most recent
    role:"assistant" entry, never tool_use blocks."""
    if not transcript_path:
        return ""
    path = Path(transcript_path)
    if not path.exists():
        return ""
    last_text = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("role") != "assistant":
                continue
            content = entry.get("message", {}).get("content", [])
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                last_text = "\n".join(texts)
    except OSError:
        return ""
    return last_text


def run(payload: dict) -> tuple[str, str, bool, str]:
    """Returns (response_text, user_prompt, is_final_stop, api_error_text).

    Cursor fires "stop" at the end of a turn (session still running) and
    "sessionEnd" when the session fully ends (the preceding stop already wrote
    the last response)."""
    if payload.get("hook_event_name") == "sessionEnd":
        return "", "", True, ""
    return response_from_transcript(payload.get("transcript_path", "")), "", False, ""
