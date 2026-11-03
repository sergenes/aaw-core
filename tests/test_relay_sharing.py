"""Session sharing (0.1.13): a guest token scoped to one session sees that session and
nothing else of the computer, and the owner's phone mints, lists and revokes guests."""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from aaw_core.relay.server import STORE_VERSION, create_app
from tests.test_relay import RecordingPush, alert, event, hello_computer, hello_phone, register_phone


@pytest.fixture
def relay(tmp_path):
    push = RecordingPush()
    app = create_app(str(tmp_path / "relay.sqlite"), push=push)
    with TestClient(app) as client:
        client.push = push
        client.db = str(tmp_path / "relay.sqlite")
        yield client


def recv_type(ws, kind: str) -> dict:
    """The next frame of this type; a full phone also receives every forwarded event and state."""
    for _ in range(20):
        frame = ws.receive_json()
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind} frame")


def share(owner, project_id="proj", guest_token="gtok", req="s1", **extra):
    owner.send_json({"type": "share", "req": req, "project_id": project_id, "guest_token": guest_token, **extra})
    reply = recv_type(owner, "shares")
    assert reply["req"] == req, reply
    return reply["shares"]


def seed(comp):
    """Two sessions on the computer, each with a project doc and a message."""
    for pid in ("proj", "other"):
        comp.send_json({"type": "state", "project_id": pid, "fields": {"status": "idle", "agent": "claude"}})
        comp.send_json({"type": "event", "project_id": pid, "event": event(f"e-{pid}", f"hi from {pid}")})
    comp.send_json({"type": "ping"})
    assert comp.receive_json() == {"type": "pong"}


def test_owner_mints_a_guest_who_sees_only_the_shared_session(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner:
        hello_computer(comp)
        register_phone(comp, "ptok")
        hello_phone(owner, "ptok")
        seed(comp)
        shares = share(owner, label_enc="enc:alex")
        assert len(shares) == 1 and shares[0]["label_enc"] == "enc:alex" and shares[0]["last_seen"] == 0
        assert len(shares[0]["id"]) == 16 and shares[0]["project_id"] == "proj"
        # the overview: every guest on the computer, each with its session
        share(owner, project_id="other", guest_token="gtok2", req="s2")
        owner.send_json({"type": "shares", "req": "all"})
        reply = recv_type(owner, "shares")
        assert "project_id" not in reply and [s["project_id"] for s in reply["shares"]] == ["other", "proj"]
        owner.send_json({"type": "shares", "req": "one", "project_id": "proj"})
        assert [s["project_id"] for s in recv_type(owner, "shares")["shares"]] == ["proj"]

        with relay.websocket_connect("/v1/ws") as guest:
            assert hello_phone(guest, "gtok") == {"type": "welcome", "computer_id": "c1", "proto": 1, "scope": "proj"}
            # the listing holds the one session
            guest.send_json({"type": "projects", "req": "p1"})
            assert set(guest.receive_json()["projects"]) == {"proj"}
            # its own history, yes; the other session's, no
            guest.send_json({"type": "history", "req": "h1", "project_id": "proj"})
            assert [e["id"] for e in guest.receive_json()["events"]] == ["e-proj"]
            guest.send_json({"type": "history", "req": "h2", "project_id": "other"})
            err = guest.receive_json()
            assert err["type"] == "error" and err["req"] == "h2" and "shared session" in err["message"]
            # no browsing, no new sessions, no deleting
            for frame in ({"type": "request", "req": "r1", "kind": "fs_browse", "payload": {}},
                          {"type": "request", "req": "r2", "kind": "new_session", "payload": {}},
                          {"type": "project_delete", "project_id": "proj"},
                          {"type": "share", "req": "s9", "project_id": "proj", "guest_token": "evil"}):
                guest.send_json(frame)
                assert guest.receive_json()["type"] == "error"
            # the computer never saw a request
            comp.send_json({"type": "ping"})
            assert comp.receive_json() == {"type": "pong"}
            # a prompt into the shared session reaches the computer like any other
            guest.send_json({"type": "command", "project_id": "proj",
                             "command": {"id": "cmd1", "ts": 2, "type": "prompt", "payload": {"args_enc": "x"}}})
            got = comp.receive_json()
            assert got["type"] == "command" and got["command"]["id"] == "cmd1"
            # the owner's socket still sees everything
            owner.send_json({"type": "projects", "req": "p2"})
            assert set(recv_type(owner, "projects")["projects"]) == {"proj", "other"}


def test_guest_is_forwarded_and_pushed_only_its_session(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as owner, relay.websocket_connect("/v1/ws") as guest:
            hello_phone(owner, "ptok")
            share(owner)
            hello_phone(guest, "gtok", platform="ios", push_token="guest-push")
            comp.send_json({"type": "event", "project_id": "other", "event": event("e1", "private")})
            comp.send_json({"type": "state", "project_id": "other", "fields": {"status": "running"}})
            comp.send_json({"type": "event", "project_id": "proj", "event": event("e2", "shared")})
            comp.send_json({"type": "computer", "fields": {"name": "box"}})
            frames = [guest.receive_json(), guest.receive_json()]
            assert [f["type"] for f in frames] == ["event", "computer"]
            assert frames[0]["project_id"] == "proj" and frames[0]["event"]["id"] == "e2"
            # a response to another phone's browse is not for the guest either
            owner.send_json({"type": "request", "req": "b1", "kind": "fs_browse", "payload": {}})
            req = comp.receive_json()
            comp.send_json({"type": "response", "req": req["req"], "kind": "fs_browse", "payload": {"entries": []}})
            recv_type(owner, "response")
            guest.send_json({"type": "ping"})
            assert guest.receive_json() == {"type": "pong"}  # nothing was queued before it
        # every phone offline: a push for the other session skips the guest, one for the shared session reaches it
        comp.send_json({"type": "event", "project_id": "other", "event": alert("n1")})
        comp.send_json({"type": "event", "project_id": "proj", "event": alert("n2")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
    pushed = [(c["push_token"], c["project_id"]) for c in relay.push.calls]
    assert ("guest-push", "proj") in pushed and ("guest-push", "other") not in pushed


def test_revoke_closes_the_guest_and_its_token_stops_working(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner:
        hello_computer(comp)
        register_phone(comp, "ptok")
        hello_phone(owner, "ptok")
        share_id = share(owner)[0]["id"]
        with relay.websocket_connect("/v1/ws") as guest:
            hello_phone(guest, "gtok")
            owner.send_json({"type": "revoke_share", "req": "v1", "project_id": "proj", "share_id": share_id})
            reply = recv_type(owner, "shares")
            assert reply["shares"] == []
            closed = guest.receive_json()
            assert closed["type"] == "error" and closed["message"] == "access revoked"
        with relay.websocket_connect("/v1/ws") as again:
            assert again_refused(hello_phone(again, "gtok"))
        # a guest token cannot be minted twice, nor collide with an existing token
        owner.send_json({"type": "share", "req": "s2", "project_id": "proj", "guest_token": "ptok"})
        assert recv_type(owner, "error")["req"] == "s2"


def again_refused(reply: dict) -> bool:
    return reply["type"] == "error" and "unknown phone token" in reply["message"]


def test_only_a_full_phone_may_share_and_the_computer_may_not(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        comp.send_json({"type": "share", "req": "s1", "project_id": "proj", "guest_token": "gtok"})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}  # silently ignored, as other phone-only frames are
    with relay.websocket_connect("/v1/ws") as ws:
        assert again_refused(hello_phone(ws, "gtok"))


def test_v3_store_gains_the_guest_columns(tmp_path):
    db = str(tmp_path / "relay.sqlite")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE devices (token TEXT PRIMARY KEY, computer_id TEXT NOT NULL, role TEXT NOT NULL,
          computer_name TEXT DEFAULT '', platform TEXT DEFAULT '', push_token TEXT DEFAULT '',
          last_seen INTEGER DEFAULT 0);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES('store_version', '3');
        INSERT INTO devices(token, computer_id, role) VALUES('aa', 'c1', 'phone');
    """)
    con.commit()
    con.close()
    with TestClient(create_app(db)):
        pass
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()[0] == str(STORE_VERSION)
    assert con.execute("SELECT scope, label_enc, created FROM devices").fetchone() == ("", "", 0)
    assert (tmp_path / f"relay.sqlite.pre-v{STORE_VERSION}").exists()
