"""The daemon's relay socket keeps a frame the socket refused and resends it next time."""

from __future__ import annotations

import asyncio
import json

import pytest

from aaw_core.transport.relay import RelayTransport


class _Socket:
    """A fake socket: `fail_first` sends raise once; iteration ends when `close` is set."""

    def __init__(self, *, fail_first: bool = False):
        self.fail_first = fail_first
        self.sent: list[dict] = []
        self.close = asyncio.Event()

    async def send(self, text: str) -> None:
        if self.fail_first:
            self.fail_first = False
            raise OSError("broken pipe")
        self.sent.append(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.close.wait()
        raise StopAsyncIteration


def test_a_refused_frame_is_resent_on_the_next_socket(tmp_path):
    """Review R04 (2026-10-23): the sender took the frame off the queue and marked it done
    even when ws.send failed, so an event could vanish from the feed and a consumed-mark
    could fail to reach the relay. Now the frame waits for the next socket, and the failed
    sender ends the session instead of sitting behind a receiver that never notices."""
    t = RelayTransport(relay_url="wss://example/v1/ws", token="t", computer_id="c", project_id="p",
                       sessions_dir=tmp_path / "sessions")

    async def run():
        t._loop = asyncio.get_running_loop()
        t._queue = asyncio.Queue()
        frame = {"type": "state", "project_id": "p", "fields": {"status": "idle"}}
        t._queue.put_nowait(frame)

        broken = _Socket(fail_first=True)
        with pytest.raises(OSError):
            await t._session(broken)  # the failed send ends the session at once
        assert broken.sent == [] and t._unsent == frame
        assert not t._queue.empty() or t._unsent is not None  # not lost, and join() still waits

        good = _Socket()
        session = asyncio.create_task(t._session(good))
        await asyncio.wait_for(t._queue.join(), 2)  # done only once the frame really went out
        assert good.sent == [frame] and t._unsent is None
        good.close.set()
        await asyncio.wait_for(session, 2)

    asyncio.run(run())
