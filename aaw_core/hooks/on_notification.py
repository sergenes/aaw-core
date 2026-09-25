"""Notification hook: forwards an agent's own notification (e.g. "waiting for
permission") to the phone as an info notification plus a desktop banner.

Works for Claude, Codex, Gemini, and Grok. Never blocks.
"""

from __future__ import annotations

import json
import re
import sys

from aaw_core.hooks.common import hook_log, notify, open_transport, preamble, read_payload


def is_grok_internal(agent: str, message: str) -> bool:
    """Grok fires Notification for internal session events that look like Rust debug
    structs ("SessionNotification { session_id: ... }"). Not user-facing; drop them."""
    return agent == "grok" and bool(re.match(r"^[A-Z][a-zA-Z]+\s*[\{\(]", message))


def main() -> int:
    settings, project, agent = preamble()
    payload = read_payload()
    hook_log(settings, "on_notification", f"agent={agent} payload={json.dumps(payload)}")
    message = str(payload.get("message") or f"{agent.capitalize()} sent a notification")
    if is_grok_internal(agent, message):
        return 0
    transport = open_transport(settings, project)
    try:
        notify(transport, message, "info", agent=agent)
    finally:
        if transport is not None:
            transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
