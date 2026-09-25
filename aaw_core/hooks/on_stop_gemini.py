"""Stop handler for Gemini CLI. Called by the on_stop dispatcher, never directly.

Gemini fires two distinct hook events: AfterAgent after each response turn (the
payload carries the response text) and SessionEnd when the session shuts down.
"""

from __future__ import annotations


def run(payload: dict) -> tuple[str, str, bool, str]:
    """Returns (response_text, user_prompt, is_final_stop, api_error_text)."""
    if payload.get("hook_event_name") == "AfterAgent":
        return payload.get("prompt_response", "").strip(), payload.get("prompt", "").strip(), False, ""
    return "", "", True, ""  # SessionEnd: AfterAgent already wrote the last response
