"""Stop handler for Cursor. Called by the on_stop dispatcher, never directly.

Cursor writes a structured JSONL transcript per session (role + typed content
blocks, the same shape as Claude Code's) and names it in the stop payload's
``transcript_path``. That is a clean source, not a pane scrape.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# Cursor's own name for "let Cursor pick": its store says "default", its screen says "Auto".
MODEL_NAMES = {"default": "Auto", "auto": "Auto"}


def model_name(value) -> str:
    """A Cursor model id as the phone shows it, "" for anything that is not a name."""
    value = value.strip() if isinstance(value, str) else ""
    return MODEL_NAMES.get(value, value)


def session_model(session_id: str) -> str:
    """The model a Cursor session last answered with: `lastUsedModel` in the session's own
    store (~/.cursor/chats/{workspace}/{session id}/store.db, table `meta`, one JSON value
    that Cursor writes as hex text). It follows a /model switch, and a session started
    with --model. "" when the session has no store yet or the store names no model."""
    if not session_id or "/" in session_id:
        return ""
    for store in (Path.home() / ".cursor" / "chats").glob(f"*/{session_id}/store.db"):
        try:
            conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=1)
            try:
                rows = conn.execute("SELECT value FROM meta").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for (value,) in rows:
            for decode in (lambda v: v, bytes.fromhex):
                try:
                    meta = json.loads(decode(value))
                except (ValueError, TypeError):
                    continue
                name = model_name(meta.get("lastUsedModel")) if isinstance(meta, dict) else ""
                if name:
                    return name
    return ""


def session_id_of(transcript_path: str) -> str:
    """The session a transcript belongs to: Cursor names the file after it
    (agent-transcripts/{session id}/{session id}.jsonl)."""
    return Path(transcript_path).stem if transcript_path else ""


def model_for(payload: dict) -> str:
    """The model for the session a hook fired in: its store first, else what the hook
    input names."""
    session_id = session_id_of(str(payload.get("transcript_path") or "")) or str(payload.get("conversation_id") or "")
    return session_model(session_id) or model_name(payload.get("model"))


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
