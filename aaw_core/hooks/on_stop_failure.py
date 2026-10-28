"""StopFailure hook (Claude Code): the turn ended on an API error instead of an answer.

Claude fires StopFailure, not Stop, when a turn dies on an API error (an expired login,
a connection that ran out of retries), so without this hook the phone heard nothing
(seen 2026-10-26: every phone prompt got "Login expired · Please run /login" in the
pane and the phone was silent). The payload names the error ("authentication_failed",
"server_error", ...) and carries the text Claude printed (Claude Code 2.1.312):

    {"hook_event_name": "StopFailure", "error": "authentication_failed",
     "last_assistant_message": "Please run /login · API Error: 401 Invalid bearer token"}

The feed gets the plain-language message every time, so each prompt shows its fate; the
push for an expired login goes once per session, since every prompt after it fails the
same way until the user signs in again.
"""

from __future__ import annotations

import sys

from aaw_core.hooks import on_stop_claude
from aaw_core.hooks.common import hook_log, notify, open_transport, preamble, read_payload


def main() -> int:
    settings, project, agent = preamble()
    payload = read_payload()
    error = str(payload.get("error") or "")
    text = str(payload.get("last_assistant_message") or "").strip() or error or "API error"
    kind = on_stop_claude.api_error_kind_from_stop_failure(error, text)
    message = on_stop_claude.friendly_api_error_message(kind, text)
    marker = on_stop_claude.auth_alert_file(settings, project)
    repeat = kind == "auth" and marker.exists()
    hook_log(settings, "on_stop", f"StopFailure project={project!r} agent={agent} error={error!r} kind={kind} "
                                  f"repeat={repeat} text={text[:120]!r}")
    transport = open_transport(settings, project)
    try:
        if transport is None:
            return 0
        transport.write_event("message", {"role": "assistant", "content": message, "agent": agent})
        transport.set_project_status("idle", pending_question_id="")
        if not repeat:
            notify(transport, message.split("\n\n")[0], "error", project=project, agent=agent, settings=settings)
        if kind == "auth":
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    finally:
        if transport is not None:
            transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
