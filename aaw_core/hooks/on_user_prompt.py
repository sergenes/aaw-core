"""UserPromptSubmit hook: the user's prompt appears on the phone right away.

Fires each time the user submits a prompt to Claude Code, Codex, or Gemini.
Never blocks: it always exits 0 with no output, whatever happens.
"""

from __future__ import annotations

import re
import sys

from aaw_core.hooks.common import open_transport, preamble, read_payload

# Claude Code fires UserPromptSubmit for its own synthetic turns too, e.g. a finished
# background task wrapped in <task-notification>, and a subagent's report wrapped
# in <agent-message>. Not the user's words, so they never reach the feed.
SYNTHETIC_PROMPT_PREFIXES = ("<task-notification>", "<agent-message")

# Claude Code wraps each block pasted into the terminal in <pasted_content id="..."> ...
# </pasted_content id="..."> (a random id per paste, repeated on the closing tag) before
# the prompt reaches the hook. Markup, not the user's words: the phone shows the pasted
# text itself, with whatever was typed around it.
_MARKER = r"</?pasted_content(?:[ \t]+id=\"[^\"]*\")?[ \t]*>"
# A marker alone on its line goes with the line; one inside a line goes alone.
_PASTE_MARKER_LINE = re.compile(r"^[ \t]*" + _MARKER + r"[ \t]*\n?", re.IGNORECASE | re.MULTILINE)
_PASTE_MARKER = re.compile(_MARKER, re.IGNORECASE)


def strip_paste_markers(text: str) -> str:
    """The prompt without Claude Code's <pasted_content> wrappers, every pair; the pasted
    text and whatever was typed around it stay as they were."""
    if "pasted_content" not in text:
        return text
    return _PASTE_MARKER.sub("", _PASTE_MARKER_LINE.sub("", text)).strip()


def user_prompt_from(payload: dict, agent: str) -> str:
    """The prompt text worth showing, or "" for slash commands and synthetic turns."""
    text = (payload.get("user_prompt") or payload.get("prompt") or "").strip()
    if agent == "grok":  # Grok wraps prompts in <user_query> tags
        text = re.sub(r"</?user_query>", "", text, flags=re.IGNORECASE).strip()
    if not text or text.startswith("/") or text.startswith(SYNTHETIC_PROMPT_PREFIXES):
        return ""
    return strip_paste_markers(text)


def main() -> int:
    settings, project, agent = preamble()
    text = user_prompt_from(read_payload(), agent)
    if text:
        transport = open_transport(settings, project)
        if transport is not None:
            try:
                transport.write_event("message", {"role": "user", "content": text, "agent": agent}, via="hook")
            finally:
                transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
