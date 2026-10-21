"""A dead peer never holds up the live ones: forwarding is concurrent and bounded."""

from __future__ import annotations

import asyncio
import time

from aaw_core.relay import server
from aaw_core.relay.server import Relay


class _Ws:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _Peer:
    """A connection whose send either records the frame at once or never completes."""

    def __init__(self, computer_id: str, role: str, *, stuck: bool = False):
        self.computer_id, self.role, self.stuck = computer_id, role, stuck
        self.id = f"{role}-{'stuck' if stuck else 'live'}"
        self.ws = _Ws()
        self.frames: list[dict] = []

    async def send(self, frame: dict) -> None:
        if self.stuck:
            await asyncio.Event().wait()  # a full kernel buffer nobody drains
        self.frames.append(frame)


def test_a_stuck_peer_is_dropped_and_the_live_one_still_gets_the_frame(tmp_path, monkeypatch):
    """Seen 2026-10-20: the Mac app relaunched, the old supervisor's socket never closed,
    and a phone's fs_browse hung for minutes behind it. The forward used to await each
    peer in turn with no deadline."""
    monkeypatch.setattr(server, "SEND_TIMEOUT_S", 0.2)

    async def run() -> None:
        relay = Relay(str(tmp_path / "relay.sqlite"))
        dead = _Peer("mac", "computer", stuck=True)
        live = _Peer("mac", "computer")
        relay.live["mac"] = {dead.id: dead, live.id: live}

        started = time.monotonic()
        n = await relay._forward("mac", {"type": "request", "kind": "fs_browse"}, role="computer")
        elapsed = time.monotonic() - started

        assert n == 1 and live.frames == [{"type": "request", "kind": "fs_browse"}]
        assert elapsed < 1.0  # one deadline, not minutes, and the live peer was not behind it
        assert dead.id not in relay.live["mac"] and dead.ws.closed  # reaped: the next request pays nothing
        assert await relay._forward("mac", {"type": "ping"}, role="computer") == 1
        assert await relay._forward("nobody", {"type": "x"}) == 0

    asyncio.run(run())
