"""PostToolUse hook: a one-line progress summary on the phone during long tasks.

Fires after each tool call completes and merges a "running" status with a short
summary ("Wrote main.py", "Ran: npm test") into the project doc. No push. Never
blocks: it always exits 0 so the agent continues whatever happens.
"""

from __future__ import annotations

import sys

from aaw_core.hooks.common import open_transport, preamble, read_payload


def normalize(agent: str, payload: dict) -> dict:
    """Grok uses camelCase field names; everything downstream expects snake_case."""
    if agent == "grok":
        if "toolName" in payload and "tool_name" not in payload:
            payload["tool_name"] = payload["toolName"]
        if "toolInput" in payload and "tool_input" not in payload:
            payload["tool_input"] = payload["toolInput"]
    return payload


def summarize(tool_name: str, tool_input: dict) -> str:
    """A short "what just happened" line for the status card."""
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    cmd = (tool_input.get("command") or "").strip()
    short_cmd = (cmd[:60] + "…") if len(cmd) > 60 else cmd
    if tool_name == "Write":
        return f"Wrote {fp}" if fp else "Wrote file"
    if tool_name in ("Edit", "MultiEdit", "search_replace"):  # search_replace: Grok's Edit
        return f"Edited {fp}" if fp else "Edited file"
    if tool_name in ("Bash", "Shell", "run_terminal_cmd"):  # Shell: Cursor; run_terminal_cmd: Grok
        return f"Ran: {short_cmd}" if short_cmd else "Ran command"
    if tool_name in ("Read", "read_file"):  # read_file: Grok
        return f"Read {fp}" if fp else "Read file"
    return f"Completed {tool_name}"


def main() -> int:
    settings, project, agent = preamble()
    payload = normalize(agent, read_payload())
    tool_name = payload.get("tool_name", "")
    if not tool_name:
        return 0
    transport = open_transport(settings, project)
    if transport is not None:
        try:
            transport.set_project_status("running", last_event_summary=summarize(tool_name, payload.get("tool_input") or {}))
        finally:
            transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
