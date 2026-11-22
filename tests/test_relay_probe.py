"""An unanswered request checks the socket at once instead of waiting out the keepalive."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
import websockets

from aaw_core.transport import relay as relay_mod
from aaw_core.transport.relay import RelayTransport


class _Relay:
    """A relay that welcomes a computer and answers nothing else, behind a TCP proxy that can
    go silent: bytes stop moving both ways while both ends still hold an open socket, which
    is what a dead network path looks like to the client."""

    def __init__(self):
        self.frozen = False
        self.connections = 0
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def start(self) -> _Relay:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._done.set)
        self._thread.join(5)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()

        async def relay(ws):
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello"
            self.connections += 1
            await ws.send(json.dumps({"type": "welcome"}))
            async for _ in ws:
                pass

        async def pipe(reader, writer):
            try:
                while data := await reader.read(65536):
                    while self.frozen:
                        await asyncio.sleep(0.01)
                    writer.write(data)
                    await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()

        async with websockets.serve(relay, "127.0.0.1", 0) as server:
            upstream = server.sockets[0].getsockname()[1]

            async def proxy(reader, writer):
                up_reader, up_writer = await asyncio.open_connection("127.0.0.1", upstream)
                await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

            front = await asyncio.start_server(proxy, "127.0.0.1", 0)
            self.port = front.sockets[0].getsockname()[1]
            self._ready.set()
            await self._done.wait()
            front.close()


@pytest.fixture
def relay():
    r = _Relay().start()
    yield r
    r.frozen = False
    r.stop()


def _transport(relay, tmp_path) -> RelayTransport:
    return RelayTransport(relay_url=f"ws://127.0.0.1:{relay.port}", token="t", computer_id="c", project_id="p",
                          sessions_dir=tmp_path / "sessions")


def _wait(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_an_unanswered_request_on_a_dead_socket_reconnects(relay, tmp_path, monkeypatch):
    """Seen 2026-11-21: the sockets died with the Mac awake, and nothing reconnected until
    the keepalive gave up 60 to 80 s later. Now the first unanswered request pings, and no
    pong drops the socket."""
    monkeypatch.setattr(relay_mod, "_PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr(relay_mod, "_CLOSE_TIMEOUT", 0.2)
    monkeypatch.setattr(relay_mod, "_RECONNECT_MIN", 0.05)
    t = _transport(relay, tmp_path).start()
    try:
        assert t.wait_connected(5)
        relay.frozen = True
        with pytest.raises(TimeoutError):
            t._request({"type": "projects"}, timeout=0.2)
        assert _wait(lambda: not t.connected.is_set(), 3), "the dead socket was kept"
        relay.frozen = False
        assert _wait(lambda: t.connected.is_set() and relay.connections == 2, 5), "no reconnect"
    finally:
        relay.frozen = False
        t.stop(flush_timeout=0.5)


def test_an_unanswered_request_on_a_live_socket_keeps_it(relay, tmp_path, monkeypatch):
    """A relay that is only slow to answer still answers a ping: the socket stays."""
    monkeypatch.setattr(relay_mod, "_PROBE_TIMEOUT", 0.5)
    t = _transport(relay, tmp_path).start()
    try:
        assert t.wait_connected(5)
        with pytest.raises(TimeoutError):
            t._request({"type": "projects"}, timeout=0.2)
        time.sleep(1.0)
        assert t.connected.is_set()
        assert relay.connections == 1
    finally:
        t.stop(flush_timeout=0.5)
