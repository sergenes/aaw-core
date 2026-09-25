"""Stop hook dispatcher: routes to the per-agent handler, then writes the turn's
messages and the session status, and sends the status notification.

Each per-agent handler returns (response_text, user_prompt, is_final_stop, api_error_text).
"""

from __future__ import annotations

import os
import sys

from aaw_core.hooks import on_stop_claude, on_stop_cursor, on_stop_gemini, on_stop_grok
from aaw_core.hooks.common import hook_log, notify, open_transport, preamble, read_payload, tmux_session
from aaw_core.transport.utils import clean_box_tables

LIMIT_KEYWORDS = on_stop_claude.LIMIT_KEYWORDS


def is_error_stop(reason: str, stop_type: str) -> bool:
    return ("ECONNRESET" in reason or "ECONNRESET" in stop_type
            or "error" in stop_type.lower() or "error" in reason.lower())


def status_message(*, reason: str, stop_type: str, api_error_text: str, response_text: str,
                   is_final_stop: bool, agent: str) -> tuple[str, str]:
    """The (message, level) for the end-of-turn notification, or ("", "success") for none."""
    if "ECONNRESET" in reason or "ECONNRESET" in stop_type:
        return "Connection lost (ECONNRESET). Send 'restart' to resume where I left off.", "error"
    if "error" in stop_type.lower() or "error" in reason.lower():
        first = api_error_text.split("\n")[0].strip() if api_error_text else ""
        if first:
            return ((first[:200] + "…") if len(first) > 200 else first), "warning"
        short = reason[:200] if reason else stop_type
        return f"Session ended with error: {short}. Send 'restart' to try again.", "warning"
    if api_error_text:
        # Not a completed answer: a connection drop or an expired login, so do not
        # frame it as "Response ready".
        first = api_error_text.split("\n")[0].strip()
        return ((first[:200] + "…") if len(first) > 200 else first), "warning"
    if response_text:
        first = response_text.split("\n")[0].strip()
        summary = (first[:120] + "…") if len(first) > 120 else first
        return f"Response ready: {summary}", "success"
    if is_final_stop:
        return f"{agent.capitalize()} session ended. Send 'restart' to start a new session.", "success"
    return "", "success"  # a non-final stop with nothing captured (e.g. a Grok intermediate step)


def main() -> int:
    settings, project, agent = preamble()
    # Grok sets GROK_HOOK_EVENT on every Stop/SessionEnd; use it when the agent
    # variable was not inherited from the tmux environment.
    if os.environ.get("GROK_HOOK_EVENT") is not None and agent != "grok":
        agent = "grok"
    payload = read_payload()
    reason, stop_type = str(payload.get("reason", "")), str(payload.get("stop_type", ""))
    is_error = is_error_stop(reason, stop_type)

    response_text = user_prompt = api_error_text = ""
    is_final_stop = True
    transport = open_transport(settings, project)
    try:
        if not is_error:
            if agent == "gemini":
                response_text, user_prompt, is_final_stop, api_error_text = on_stop_gemini.run(payload)
            elif agent == "grok":
                response_text, user_prompt, is_final_stop, api_error_text = on_stop_grok.run(payload, settings, project)
            elif agent == "cursor":
                response_text, user_prompt, is_final_stop, api_error_text = on_stop_cursor.run(payload)
            else:  # claude, codex, scoot, or unknown
                response_text, user_prompt, is_final_stop, api_error_text = on_stop_claude.run(
                    payload, settings, project, agent)
            hook_log(settings, "on_stop",
                     f"stop_type={stop_type!r} project={project!r} agent={agent} "
                     f"transcript={'yes' if payload.get('transcript_path') else 'no'} "
                     f"user_prompt={bool(user_prompt)} response={bool(response_text)} final={is_final_stop}")
            if transport is None:
                hook_log(settings, "on_stop", "transport is None (not linked?)")
            else:
                if user_prompt:
                    transport.write_event("message", {"role": "user", "content": user_prompt, "agent": agent})
                if response_text:
                    transport.write_event("message", {"role": "assistant", "content": clean_box_tables(response_text),
                                                      "agent": agent})
                    if any(kw in response_text.lower() for kw in LIMIT_KEYWORDS):
                        # A rate-limit message: a reminder event lets the phone offer a
                        # local notification at reset time.
                        reset_unix, reset_label = on_stop_claude.parse_reset_info(response_text)
                        if reset_unix:
                            transport.write_event("reminder", {"reset_at": reset_unix, "reset_label": reset_label,
                                                               "agent": agent})
                transport.set_project_status("stopped" if is_final_stop else "idle", pending_question_id="")
        else:
            # The stop carries an error reason: forward what is in the pane (e.g. "API Error:
            # SSO session expired") so the phone sees it.
            session = tmux_session(project)
            api_error_text = on_stop_claude.api_error_from_tmux(session) or on_stop_claude.response_from_tmux(session)
            if api_error_text and transport is not None:
                transport.write_event("message", {"role": "assistant", "content": api_error_text, "agent": agent})
                transport.set_project_status("stopped", pending_question_id="")
                hook_log(settings, "on_stop", f"error forwarded: {api_error_text[:80]}")

        message, level = status_message(reason=reason, stop_type=stop_type, api_error_text=api_error_text,
                                        response_text=response_text, is_final_stop=is_final_stop, agent=agent)
        if message:
            notify(transport, message, level)
    finally:
        if transport is not None:
            transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
