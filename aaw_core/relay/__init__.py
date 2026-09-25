"""The relay: a small WebSocket server that connects headless hosts to phones.

Run it with ``python -m aaw_core.relay``; build the ASGI app with ``create_app``.
"""

from aaw_core.relay.server import NullPushSender, PushSender, Relay, create_app

__all__ = ["NullPushSender", "PushSender", "Relay", "create_app"]
