"""PreToolUse hook: intercepts Write / Edit / Bash and asks the phone for approval.

Behaviour depends on mobile mode:
  mobile mode ON:  ask for approval on the phone and block until answered.
  mobile mode OFF: say nothing, so the agent shows its own desktop dialog.

Mobile mode is enabled when the user sends anything from the phone (the daemon
sets the mobile_mode file) and expires after phone inactivity, or is "manual".
Whatever happens, a failure here exits silently so the agent falls back to its
own dialog; it never leaves the phone with a stale question card.
"""

from __future__ import annotations

import json
import sys
import time

from aaw_core.config import Settings
from aaw_core.hooks.common import (
    hook_log,
    is_mobile_mode,
    open_transport,
    preamble,
    read_payload,
    refresh_mobile_mode,
)

OPTIONS = ["Yes", "Yes for this session", "No"]
ANSWER_TIMEOUT_S = 300
PENDING_WAIT_S = 300

# Read-only tools that would otherwise flood the phone with permission cards.
READONLY = {
    "gemini": {"read_file", "list_directory", "search_files", "glob", "get_file_info", "list_files"},
    "grok": {"read_file", "list_dir", "grep", "web_search", "web_fetch", "glob"},
}


# ── per-agent hook output (each agent has its own JSON dialect) ─────────────


def approve_output(agent: str) -> tuple[str, int]:
    """(stdout, exit code) that approves the tool call for this agent."""
    if agent == "codex":
        return json.dumps({"hookSpecificOutput": {"permissionDecision": "approve"}}), 0
    if agent == "gemini":
        return json.dumps({"decision": "allow"}), 0
    if agent == "cursor":
        return json.dumps({"permission": "allow"}), 0
    if agent == "scoot":
        return json.dumps({"permissionDecision": "allow"}), 0
    return json.dumps({"decision": "approve"}), 0  # Claude Code: "approve", not "allow"


def block_output(agent: str, reason: str = "Denied from mobile app.") -> tuple[str, int]:
    """(stdout, exit code) that blocks the tool call for this agent."""
    if agent == "codex":
        return json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",
                                                  "permissionDecisionReason": reason}}), 0
    if agent == "grok":
        return json.dumps({"decision": "block", "reason": reason}), 2  # Grok needs exit 2 to deny
    if agent == "gemini":
        return json.dumps({"decision": "deny", "reason": reason}), 0
    if agent == "cursor":
        return json.dumps({"permission": "deny", "user_message": reason}), 0
    if agent == "scoot":
        return json.dumps({"permissionDecision": "deny", "reason": reason}), 0
    return json.dumps({"decision": "block", "reason": reason}), 0  # Claude Code: "block"


# ── payload normalization + the question text ───────────────────────────────


def normalize_payload(agent: str, payload: dict) -> dict:
    """Bring every agent's payload to the tool_name / tool_input shape."""
    if agent == "grok":
        if "toolName" in payload and "tool_name" not in payload:
            payload["tool_name"] = payload["toolName"]
        if "toolInput" in payload and "tool_input" not in payload:
            payload["tool_input"] = payload["toolInput"]
    elif agent == "cursor" and payload.get("hook_event_name") == "beforeShellExecution":
        # A separate hook event with a bare top-level "command"; without it Cursor's own
        # "Not in allowlist" prompt still appears for shell commands.
        payload["tool_name"] = "Shell"
        payload["tool_input"] = {"command": payload.get("command", "")}
    elif agent == "cursor" and payload.get("hook_event_name") == "beforeMCPExecution":
        # A third event for MCP-backed tools (e.g. web search); tool_input is a JSON string.
        payload["tool_name"] = f"MCP:{payload.get('tool_name', 'MCP tool')}"
        raw = payload.get("tool_input", "")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {"raw": raw}
        payload["tool_input"] = raw if isinstance(raw, dict) else {}
    return payload


def build_question(tool_name: str, tool_input: dict) -> str:
    """The human-readable question on the permission card."""
    file_path = tool_input.get("file_path", "")
    cmd = (tool_input.get("command") or "").strip()
    if tool_name in ("Write", "write_file"):
        return f"Create / overwrite: {file_path or 'a file'}"
    if tool_name in ("Edit", "MultiEdit", "replace_in_file"):
        return f"Edit: {file_path or 'a file'}"
    if tool_name in ("Bash", "run_shell_command", "Shell", "run_terminal_cmd"):
        return f"Run: {cmd[:140]}"
    if tool_name == "search_replace":  # Grok's Edit
        return f"Edit: {tool_input.get('file_path', tool_input.get('path', '')) or 'a file'}"
    if tool_name.startswith("MCP:"):
        detail = tool_input.get("query") or tool_input.get("url") or tool_input.get("command") or ""
        return f"Allow {tool_name[4:]}?" + (f" ({detail[:100]})" if detail else "")
    return f"Allow {tool_name}?"


# ── the decision ────────────────────────────────────────────────────────────


def decide(settings: Settings, project: str, agent: str, payload: dict) -> tuple[str, int]:
    """Ask the phone and return (stdout, exit code). ("", 0) means "say nothing"."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool_name in READONLY.get(agent, ()):
        return approve_output(agent)
    question = build_question(tool_name, tool_input)

    transport = open_transport(settings, project)
    if transport is None:
        return approve_output(agent)
    try:
        try:
            doc = transport.get_project()
        except Exception as e:  # noqa: BLE001 - a failed read must not block the agent
            hook_log(settings, "on_pre_tool", f"project read failed: {e}")
            doc = {}
        hook_log(settings, "on_pre_tool", f"mobile_mode=ON tool={tool_name!r} auto_approve={doc.get('auto_approve')!r}")
        if doc.get("auto_approve"):
            return approve_output(agent)

        # Some agents fire more than one tool hook at once (Cursor: a Shell + Grep pair
        # within 1ms). pending_question_id is a single field, so a second question would
        # overwrite the first and one hook would poll forever. Wait for any in-flight
        # question to clear so the phone shows one at a time and every hook gets an answer.
        if doc.get("pending_question_id"):
            hook_log(settings, "on_pre_tool", "another question is pending; waiting for it to clear")
            deadline = time.time() + PENDING_WAIT_S
            while time.time() < deadline:
                time.sleep(1)
                try:
                    if not transport.get_project().get("pending_question_id"):
                        break
                except Exception:  # noqa: BLE001
                    break
            else:
                hook_log(settings, "on_pre_tool", "pending question never cleared; sending anyway")

        hook_log(settings, "on_pre_tool", f"sending question to mobile: {question!r}")
        transport.send_question(project=project, agent=agent, question=question, options=OPTIONS,
                                timeout_s=ANSWER_TIMEOUT_S)
        answer = transport.poll_answer(time.time() + ANSWER_TIMEOUT_S)
        if answer is not None:
            refresh_mobile_mode(settings)  # answering counts as phone activity
        hook_log(settings, "on_pre_tool", f"got answer: {answer!r}")

        if answer is None:
            # Nobody answered within the window. An arriving answer clears
            # pending_question_id (_take_answer); on a timeout only this hook can, and
            # leaving it set keeps a zombie card on the phone and makes the next
            # question wait out PENDING_WAIT_S behind the stale id.
            transport.set_project_status("running", pending_question_id="")
            transport.write_notification(
                f"No answer in {ANSWER_TIMEOUT_S // 60} minutes; approved and continued: {question}",
                level="info")
        if answer == "No":
            return block_output(agent)
        if answer == "Yes for this session":
            transport.update_project(auto_approve=True)
            transport.set_project_status("running", pending_question_id="")
        return approve_output(agent)
    except Exception as e:  # noqa: BLE001 - never block the agent; clear our question and fall back
        hook_log(settings, "on_pre_tool", f"EXCEPTION: {e}; exiting silently (desktop shows its dialog)")
        try:
            transport.set_project_status("running", pending_question_id="")
        except Exception as e2:  # noqa: BLE001
            hook_log(settings, "on_pre_tool", f"could not clear the pending question: {e2}")
        return "", 0
    finally:
        transport.stop()


def main() -> int:
    settings, project, agent = preamble()
    if not is_mobile_mode(settings):
        hook_log(settings, "on_pre_tool", "mobile_mode=OFF; exit (desktop shows its dialog)")
        return 0
    out, code = decide(settings, project, agent, normalize_payload(agent, read_payload()))
    if out:
        print(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
