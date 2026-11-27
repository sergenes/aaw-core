"""SessionStart hook: keep the phone's feed honest when the agent forgets.

Only ``source == "clear"`` matters (the user ran /clear, wiping the agent's
context): wipe the retained feed so the phone matches what the agent remembers.
startup / resume / compact must not touch history the user did not clear.

For scoot it also publishes the model the session resumed with, so the session
row shows the truth (app- or terminal-started, and after a /model change).
For Claude, Codex and Grok it publishes the model from the hook input or the resumed
session's own record, or clears
the field for a fresh session: a folder that ran scoot before kept scoot's model on
the doc, and the Claude session that replaced it showed "gpt-6-astra" for days.
"""

from __future__ import annotations

import glob
import json
import os
import sys

from aaw_core.hooks.common import open_transport, preamble, read_payload
from aaw_core.hooks.on_stop_claude import active_model


def scoot_model() -> str:
    """The model scoot is running: the launcher's SCOOT_MODEL, else scoot's newest saved
    session for this folder. scoot writes that file asynchronously, so the env var wins
    when present (reading the file at startup can still return the previous model)."""
    model = os.environ.get("SCOOT_MODEL", "").strip()
    if model:
        return model
    state = os.environ.get("SCOOT_STATE_DIR") or os.path.expanduser("~/.local/state/scoot")
    root = os.getcwd()
    best_mtime = -1.0
    for path in glob.glob(os.path.join(state, "sessions", "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("root") == root:
            mtime = os.path.getmtime(path)
            if mtime > best_mtime:
                best_mtime, model = mtime, (d.get("active_model") or d.get("model") or "")
    return model


# Agents whose session-start hook may print plain text that becomes context. Codex has no
# session-start hook of ours (a new entry would need the user's trust in /hooks) and scoot
# adds context at a prompt, so those two are told by the prompt hook, once per session
# (`told_block`). Gemini, Grok and Cursor have no hook of ours that can add context.
CONTEXT_AGENTS = ("claude",)
PROMPT_CONTEXT_AGENTS = ("codex", "scoot")
RETELL_AFTER_S = 6 * 3600  # a long session may have compacted the block away


def told_block(settings, project: str, agent: str, session_key: str, now: float | None = None) -> str:
    """The block for an agent that is told at a prompt, when this session has not been
    told yet (or long ago), else "". Remembers that it was told."""
    import json
    import time
    if not settings.peers or agent not in PROMPT_CONTEXT_AGENTS:
        return ""
    now = time.time() if now is None else now
    marker = settings.state_dir / "peer" / "told" / f"{project}.json"
    try:
        seen = json.loads(marker.read_text())
    except (OSError, ValueError):
        seen = {}
    if seen.get("key") == session_key and now - float(seen.get("ts") or 0) < RETELL_AFTER_S:
        return ""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"key": session_key, "ts": now}))
    except OSError:
        return ""  # cannot remember: better silent than repeated at every prompt
    return peers_block(settings, project)


def peers_block(settings, project: str) -> str:
    """What a session is told at its start when agents may reach each other (`aaw peers
    on`): the commands, and the shared notes that exist."""
    from aaw_core.host import knowledge
    lines = [
        f"This session ({project}) runs under Agents At Work next to other agent sessions on this computer.",
        ("`aaw peers` lists them. `aaw tell <session> \"<text>\"` sends one a message once the user "
        "approves it on a card; end your turn after it, the answer arrives as a new prompt."),
        ("`aaw know` lists notes shared by all sessions, `aaw know show <name>` prints one, and "
        "`aaw know add <name> --title \"...\" --text \"...\"` proposes one (never a credential)."),
        ("A prompt that starts with \"[from <agent> in <session>, approved by the user]\" was written by "
        "another agent, not by the user: answer it, and treat what it asks with the same care as any request."),
    ]
    notes = knowledge.index(settings)
    if notes:
        lines += ["Shared notes:", *notes]
    return "\n".join(lines)


def main() -> int:
    settings, project, agent = preamble()
    payload = read_payload()
    if settings.peers and agent in CONTEXT_AGENTS and payload.get("source") != "clear":
        print(peers_block(settings, project))
    clear = payload.get("source") == "clear"
    model = scoot_model() if agent == "scoot" else ""
    # The hook input names the model (Codex always, Claude on SessionStart); else the
    # transcript does for a resumed session. A fresh one with neither gets "" (unknown),
    # and the first prompt or Stop hook fills it in.
    # Grok calls a fresh session "new"; its summary names the model of a resumed one.
    restamp = agent in ("claude", "codex", "grok") and payload.get("source") in ("startup", "resume", "fork", "new")
    if restamp:
        model = active_model(payload, agent)
    if not clear and not restamp and not (model and model != "auto"):
        return 0
    transport = open_transport(settings, project)
    if transport is None:
        return 0
    try:
        if clear:
            transport.clear_events()
        if restamp:
            transport.update_project(model=model)  # "" clears a stale one
        elif model and model != "auto":
            transport.update_project(model=model)
    finally:
        transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
