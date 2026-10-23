"""The daemon's relay socket gives a slow host room: handshake and ping limits."""

from __future__ import annotations

import asyncio

from aaw_core.transport import relay as relay_mod
from aaw_core.transport.relay import RelayTransport


def test_connect_uses_the_slow_host_limits(tmp_path, monkeypatch):
    """A loaded Mac missed the library's 10 s handshake 1,725 times and took twenty minutes
    to reconnect after a wake (2026-10-23). The limits are explicit and wide."""
    seen: dict = {}

    class _Connect:
        def __init__(self, url, **kwargs):
            seen.update(kwargs); seen["url"] = url

        async def __aenter__(self):
            raise ConnectionError("not today")

        async def __aexit__(self, *a):
            return False

    t = RelayTransport(relay_url="wss://example/v1/ws", token="t", computer_id="c", project_id="p",
                       sessions_dir=tmp_path / "sessions")
    monkeypatch.setattr(relay_mod.websockets, "connect", _Connect)
    monkeypatch.setattr(relay_mod, "_RECONNECT_MIN", 0.01)

    async def run():
        task = asyncio.create_task(t._run())
        await asyncio.sleep(0.05)  # one failed attempt is enough
        t._stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert seen["url"] == "wss://example/v1/ws"
    assert seen["open_timeout"] == relay_mod._OPEN_TIMEOUT == 30.0
    assert seen["ping_interval"] == relay_mod._PING_INTERVAL == 20.0
    assert seen["ping_timeout"] == relay_mod._PING_TIMEOUT == 60.0
