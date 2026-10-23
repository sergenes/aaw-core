"""SessionStart hook: keep the phone's feed honest when the agent forgets.

Only ``source == "clear"`` matters (the user ran /clear, wiping the agent's
context): wipe the retained feed so the phone matches what the agent remembers.
startup / resume / compact must not touch history the user did not clear.

For scoot it also publishes the model the session resumed with, so the session
row shows the truth (app- or terminal-started, and after a /model change).
For Claude and Codex it publishes the model from the hook input or the resumed
transcript, or clears
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


def main() -> int:
    settings, project, agent = preamble()
    payload = read_payload()
    clear = payload.get("source") == "clear"
    model = scoot_model() if agent == "scoot" else ""
    # The hook input names the model (Codex always, Claude on SessionStart); else the
    # transcript does for a resumed session. A fresh one with neither gets "" (unknown),
    # and the first prompt or Stop hook fills it in.
    restamp = agent in ("claude", "codex") and payload.get("source") in ("startup", "resume", "fork")
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
