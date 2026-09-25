"""UserPromptSubmit hook: the user's prompt appears on the phone right away.

Fires each time the user submits a prompt to Claude Code, Codex, or Gemini.
Never blocks: it always exits 0 with no output, whatever happens.
"""

from __future__ import annotations

import re
import sys

from aaw_core.hooks.common import open_transport, preamble, read_payload

# Claude Code fires UserPromptSubmit for its own synthetic turns too, e.g. a finished
# background task delivered back wrapped in <task-notification>. Not the user's words.
SYNTHETIC_PROMPT_PREFIXES = ("<task-notification>",)


def user_prompt_from(payload: dict, agent: str) -> str:
    """The prompt text worth showing, or "" for slash commands and synthetic turns."""
    text = (payload.get("user_prompt") or payload.get("prompt") or "").strip()
    if agent == "grok":  # Grok wraps prompts in <user_query> tags
        text = re.sub(r"</?user_query>", "", text, flags=re.IGNORECASE).strip()
    if not text or text.startswith("/") or text.startswith(SYNTHETIC_PROMPT_PREFIXES):
        return ""
    return text


def main() -> int:
    settings, project, agent = preamble()
    text = user_prompt_from(read_payload(), agent)
    if text:
        transport = open_transport(settings, project)
        if transport is not None:
            try:
                transport.write_event("message", {"role": "user", "content": text, "agent": agent})
            finally:
                transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
