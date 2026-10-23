"""The daemon's echo of a delivered prompt names the command it came from."""

from __future__ import annotations

from aaw_core import daemon


class _Transport:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def write_event(self, event_type, payload, via=None):
        self.events.append((event_type, payload))

    def write_notification(self, message, level="info", set_running=False):
        self.events.append(("notification", {"message": message, "level": level}))


def test_echo_carries_the_command_id(monkeypatch):
    """Review R08 (2026-10-23): the phones matched their prompt to the daemon's echo by
    text, so two prompts saying the same thing collapsed into one row. With the id in
    the echo, the match is exact; a terminal-typed prompt has no command and no id."""
    monkeypatch.setattr(daemon.tmux, "send_text", lambda text, session, agent: True)
    t = _Transport()
    daemon.forward_text(t, "aaw-p", "claude", "continue", command_id="cmd-1")
    daemon.forward_text(t, "aaw-p", "claude", "continue")
    assert t.events[0] == ("message", {"role": "user", "content": "continue", "agent": "claude", "command_id": "cmd-1"})
    assert "command_id" not in t.events[1][1]
