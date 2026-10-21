"""Only the supervisor writes the computer's name into its document."""

from __future__ import annotations

from aaw_core.transport.relay import RelayTransport


def _transport(tmp_path, **kw) -> tuple[RelayTransport, list]:
    t = RelayTransport(relay_url="wss://example/v1/ws", token="t", computer_id="c", project_id="p",
                       sessions_dir=tmp_path / "sessions", computer_name="Serge's Mac", **kw)
    sent: list[dict] = []
    t._send = sent.append  # type: ignore[method-assign]
    return t, sent


def test_only_an_announcing_transport_writes_the_name(tmp_path):
    """Session daemons heartbeat too; each resolving the name on its own had the phones flip
    between two spellings with every heartbeat. A daemon's heartbeat carries no name."""
    supervisor, sent = _transport(tmp_path)
    supervisor.update_computer(status="online")
    assert sent[-1]["fields"]["name"] == "Serge's Mac"
    daemon, sent = _transport(tmp_path, announces_name=False)
    daemon.update_computer(status="running", daemon_version="0.1.9")
    assert "name" not in sent[-1]["fields"]
    assert sent[-1]["fields"]["status"] == "running" and sent[-1]["fields"]["daemon_version"] == "0.1.9"
