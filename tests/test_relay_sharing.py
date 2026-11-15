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
            # no new sessions, no deleting, no sharing (file requests: see the 0.1.16 tests below)
            for frame in ({"type": "request", "req": "r2", "kind": "new_session", "payload": {}},
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


# ── 0.1.16: who sent it ───────────────────────────────────────────────────────


def command(cid: str, **extra) -> dict:
    return {"id": cid, "type": "command", "ts": 1, "consumed": False,
            "payload": {"command": "text", "args": "", "args_enc": "enc:hi"}, **extra}


def barrier(ws) -> None:
    """Wait until the relay has processed everything this socket sent so far."""
    ws.send_json({"type": "ping"})
    recv_type(ws, "pong")


def shared_session(relay, comp, owner, guest, label="enc:anna"):
    hello_computer(comp)
    register_phone(comp, "ptok")
    hello_phone(owner, "ptok")
    seed(comp)
    recv_type(owner, "event"), recv_type(owner, "event")
    share_id = share(owner, label_enc=label)[0]["id"]
    assert hello_phone(guest, "gtok", platform="ios")["scope"] == "proj"
    return share_id


def test_a_guests_command_is_stamped_with_its_share_and_the_owners_is_not(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        share_id = shared_session(relay, comp, owner, guest)
        guest.send_json({"type": "command", "project_id": "proj", "command": command("g1")})
        got = recv_type(comp, "command")["command"]
        assert (got["from"], got["from_label_enc"], got["from_platform"]) == (share_id, "enc:anna", "ios")
        assert recv_type(owner, "command")["command"]["from"] == share_id  # the owner's phone sees it too

        owner.send_json({"type": "command", "project_id": "proj", "command": command("o1")})
        got = recv_type(comp, "command")["command"]
        assert not {"from", "from_label_enc", "from_platform"} & set(got)

        # stored as stamped: a daemon that connects later reads the same sender
        comp.send_json({"type": "commands", "req": "c", "project_id": "proj"})
        stored = {c["id"]: c for c in recv_type(comp, "commands")["commands"]}
        assert stored["g1"]["from"] == share_id and "from" not in stored["o1"]


def test_nobody_on_a_phone_can_claim_a_sender(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        share_id = shared_session(relay, comp, owner, guest)
        forged = {"from": "0" * 16, "from_label_enc": "enc:the owner", "from_platform": "android"}
        guest.send_json({"type": "command", "project_id": "proj", "command": command("g1", **forged)})
        got = recv_type(comp, "command")["command"]
        assert (got["from"], got["from_label_enc"], got["from_platform"]) == (share_id, "enc:anna", "ios")
        owner.send_json({"type": "command", "project_id": "proj", "command": command("o1", **forged)})
        assert not {"from", "from_label_enc", "from_platform"} & set(recv_type(comp, "command")["command"])
        # nor through an update
        guest.send_json({"type": "command_update", "project_id": "proj", "command_id": "o1",
                         "fields": {"from": "0" * 16, "deliver_at": 5}})
        assert recv_type(comp, "command_update")["fields"] == {"deliver_at": 5}


def test_a_share_without_a_label_is_stamped_with_its_id_alone(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        hello_computer(comp)
        register_phone(comp, "ptok")
        hello_phone(owner, "ptok")
        share_id = share(owner)[0]["id"]
        hello_phone(guest, "gtok")
        guest.send_json({"type": "command", "project_id": "proj", "command": command("g1")})
        got = recv_type(comp, "command")["command"]
        assert got["from"] == share_id and "from_label_enc" not in got and "from_platform" not in got


def test_the_sender_follows_whoever_last_wrote_the_prompt(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        share_id = shared_session(relay, comp, owner, guest)
        owner.send_json({"type": "command", "project_id": "proj", "command": command("q1", deliver_at=9)})
        recv_type(comp, "command")
        # the guest only moves the time: still the owner's prompt
        guest.send_json({"type": "command_update", "project_id": "proj", "command_id": "q1",
                         "fields": {"deliver_at": 10}})
        assert recv_type(comp, "command_update")["fields"] == {"deliver_at": 10}
        # the guest rewrites it: now the guest's
        guest.send_json({"type": "command_update", "project_id": "proj", "command_id": "q1",
                         "fields": {"payload": {"command": "text", "args": "", "args_enc": "enc:mine"}}})
        assert recv_type(comp, "command_update")["fields"]["from"] == share_id
        # the owner rewrites it back: the sender is cleared
        owner.send_json({"type": "command_update", "project_id": "proj", "command_id": "q1",
                         "fields": {"payload": {"command": "text", "args": "", "args_enc": "enc:back"}}})
        fields = recv_type(comp, "command_update")["fields"]
        assert (fields["from"], fields["from_label_enc"], fields["from_platform"]) == ("", "", "")
        # the engine's own update (consumed) says nothing about the sender
        comp.send_json({"type": "command_update", "project_id": "proj", "command_id": "q1",
                        "fields": {"consumed": True}})
        seen = [recv_type(owner, "command_update")["fields"] for _ in range(2)]  # the guest's two, then this
        assert seen[0] == {"deliver_at": 10} and seen[1]["from"] == share_id
        assert recv_type(owner, "command_update")["fields"] == {"consumed": True}


def test_a_message_queued_behind_a_dialog_keeps_its_sender(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        share_id = shared_session(relay, comp, owner, guest)
        guest.send_json({"type": "state", "project_id": "proj",
                         "fields": {"pending_message": "enc:wait", "pending_from": "0" * 16}})
        barrier(guest)
        comp.send_json({"type": "project", "req": "p", "project_id": "proj"})
        doc = recv_type(comp, "project")["fields"]
        assert (doc["pending_message"], doc["pending_from"], doc["pending_from_label_enc"]) == (
            "enc:wait", share_id, "enc:anna")
        owner.send_json({"type": "state", "project_id": "proj", "fields": {"pending_message": "enc:mine"}})
        barrier(owner)
        comp.send_json({"type": "project", "req": "p2", "project_id": "proj"})
        doc = recv_type(comp, "project")["fields"]
        assert doc["pending_message"] == "enc:mine" and doc["pending_from"] == ""


# ── 0.1.16: a guest's file requests ───────────────────────────────────────────


def test_a_guests_file_request_reaches_the_computer_fenced_and_answers_only_that_guest(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest, relay.websocket_connect("/v1/ws") as other:
        shared_session(relay, comp, owner, guest)
        share(owner, project_id="other", guest_token="gtok2", req="s2")
        hello_phone(other, "gtok2")
        for kind in ("fs_browse", "fs_fetch", "fs_attach"):
            guest.send_json({"type": "request", "req": "r1", "kind": kind, "payload": {"path_enc": "enc:p"}})
            got = recv_type(comp, "request")
            assert got["kind"] == "guest_" + kind and got["scope"] == "proj" and got["payload"] == {"path_enc": "enc:p"}
            assert got["req"] != "r1"  # the relay's own id: a guest cannot aim at another phone's request
            # a file in parts: every frame returns to the guest, under its own id and kind
            for part in (0, 1):
                comp.send_json({"type": "response", "req": got["req"], "kind": got["kind"], "payload": {"part": part}})
                assert guest.receive_json() == {"type": "response", "req": "r1", "kind": kind, "payload": {"part": part}}
        # nobody else heard any of it: the next frame each of them gets is its own pong
        barrier(comp)
        for ws in (owner, other):
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_a_guest_still_cannot_start_stop_or_create_sessions(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        shared_session(relay, comp, owner, guest)
        for kind in ("new_session", "start_session", "stop_session", "guest_fs_browse", None, ["fs_browse"]):
            guest.send_json({"type": "request", "req": "r", "kind": kind, "payload": {}})
            assert guest.receive_json() == {"type": "error", "message": "not allowed for a shared session", "req": "r"}
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}  # nothing was forwarded


def test_an_owners_request_is_forwarded_as_before(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        shared_session(relay, comp, owner, guest)
        owner.send_json({"type": "request", "req": "r1", "kind": "fs_browse", "payload": {}})
        got = recv_type(comp, "request")
        assert got == {"type": "request", "req": "r1", "kind": "fs_browse", "payload": {}}
        comp.send_json({"type": "response", "req": "r1", "kind": "fs_browse", "payload": {"entries": []}})
        assert recv_type(owner, "response")["req"] == "r1"
        guest.send_json({"type": "ping"})
        assert guest.receive_json() == {"type": "pong"}  # an owner's listing never reaches a guest


def test_a_guests_request_with_the_computer_offline_is_answered_by_the_relay(relay):
    with relay.websocket_connect("/v1/ws") as owner, relay.websocket_connect("/v1/ws") as guest:
        with relay.websocket_connect("/v1/ws") as comp:
            shared_session(relay, comp, owner, guest)
        guest.send_json({"type": "request", "req": "r1", "kind": "fs_browse", "payload": {}})
        assert recv_type(guest, "response") == {"type": "response", "req": "r1", "kind": "fs_browse",
                                                "payload": {"error": "offline"}}
