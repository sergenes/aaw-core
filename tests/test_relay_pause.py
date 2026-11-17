"""Pausing a shared session's notifications (0.1.18): a phone that paused is told only of
the turns it started itself, and of those nobody on a phone started."""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from aaw_core.relay import server
from aaw_core.relay.server import create_app
from tests.test_relay import RecordingPush, alert, hello_computer, hello_phone, register_phone
from tests.test_relay_sharing import barrier, command, recv_type, share

OWNER_PUSH, GUEST_PUSH = "owner-push", "guest-push"


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ACK_WAIT_S", 0.05)
    push = RecordingPush()
    app = create_app(str(tmp_path / "relay.sqlite"), push=push)
    with TestClient(app) as client:
        client.push = push
        client.db = str(tmp_path / "relay.sqlite")
        yield client


def pair(comp, owner, guest) -> str:
    """A computer, its owner's phone and a guest of session `proj`; the guest's share id."""
    hello_computer(comp)
    register_phone(comp, "ptok")
    hello_phone(owner, "ptok", platform="ios", push_token=OWNER_PUSH)
    share_id = share(owner)[0]["id"]
    hello_phone(guest, "gtok", platform="android", push_token=GUEST_PUSH)
    return share_id


def pause(phone, paused: bool | None = True, project_id: str = "proj") -> dict:
    frame = {"type": "notify_pause", "req": "np", "project_id": project_id}
    if paused is not None:
        frame["paused"] = paused
    phone.send_json(frame)
    return recv_type(phone, "notify_pause")


def prompt_echo(event_id: str, **fields) -> dict:
    """The computer's echo of a prompt it typed into the agent."""
    return {"id": event_id, "type": "message", "ts": 1, "payload": {"role": "user", "content": "enc", **fields}}


def question(event_id: str) -> dict:
    return {"id": event_id, "type": "question", "ts": 1, "payload": {"question": "enc", "kind": "permission"}}


def turn(comp, sender, command_id: str, *events: dict, **command_fields) -> None:
    """`sender` (a phone socket, or the computer itself) sends a prompt; the computer
    echoes it and then writes `events`."""
    sender.send_json({"type": "command", "project_id": "proj", "command": command(command_id, **command_fields)})
    barrier(sender)
    comp.send_json({"type": "event", "project_id": "proj", "event": prompt_echo(f"echo-{command_id}",
                                                                                command_id=command_id)})
    for e in events:
        comp.send_json({"type": "event", "project_id": "proj", "event": e})
    barrier(comp)


def pushed(relay) -> list[tuple[str, str]]:
    return [(c["push_token"], c["event"]["id"]) for c in relay.push.calls]


def offline_turns(relay, *, owner_paused: bool = False, guest_paused: bool = False):
    """Both phones pair, pause as asked and go offline; yields the computer socket and
    two sockets to send prompts with (no push token in their hello keeps the stored one)."""
    comp = relay.websocket_connect("/v1/ws").__enter__()
    with relay.websocket_connect("/v1/ws") as owner, relay.websocket_connect("/v1/ws") as guest:
        pair(comp, owner, guest)
        if owner_paused:
            assert pause(owner)["paused"] is True
        if guest_paused:
            assert pause(guest)["paused"] is True
    return comp


def test_a_paused_phone_is_not_pushed_the_other_persons_turn(relay):
    comp = offline_turns(relay, owner_paused=True)
    with relay.websocket_connect("/v1/ws") as guest:
        hello_phone(guest, "gtok")
        turn(comp, guest, "g1")
    # the guest left; her turn asks a question and finishes
    comp.send_json({"type": "event", "project_id": "proj", "event": question("q1")})
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n1")})
    barrier(comp)
    assert pushed(relay) == [(GUEST_PUSH, "q1"), (GUEST_PUSH, "n1")]
    comp.__exit__(None, None, None)


def test_the_sender_is_always_notified_even_with_its_own_pause_on(relay):
    comp = offline_turns(relay, owner_paused=True)
    with relay.websocket_connect("/v1/ws") as owner:
        hello_phone(owner, "ptok")
        turn(comp, owner, "o1")
    comp.send_json({"type": "event", "project_id": "proj", "event": question("q1")})
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n1")})
    barrier(comp)
    # his own turn: he is pushed, and so is the guest, who paused nothing
    assert sorted(pushed(relay)) == sorted([(OWNER_PUSH, "q1"), (GUEST_PUSH, "q1"),
                                            (OWNER_PUSH, "n1"), (GUEST_PUSH, "n1")])
    comp.__exit__(None, None, None)


def test_a_guest_who_paused_is_spared_the_owners_turn_and_another_guests(relay):
    comp = offline_turns(relay, guest_paused=True)
    with relay.websocket_connect("/v1/ws") as owner:
        hello_phone(owner, "ptok")
        share(owner, guest_token="gtok2", req="s2")
        turn(comp, owner, "o1")
        with relay.websocket_connect("/v1/ws") as second:
            hello_phone(second, "gtok2", platform="ios", push_token="second-push")
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n1")})
    barrier(comp)
    assert sorted(pushed(relay)) == sorted([(OWNER_PUSH, "n1"), ("second-push", "n1")])
    relay.push.calls.clear()
    with relay.websocket_connect("/v1/ws") as second:
        hello_phone(second, "gtok2")
        turn(comp, second, "s1")
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n2")})
    barrier(comp)
    assert sorted(pushed(relay)) == sorted([(OWNER_PUSH, "n2"), ("second-push", "n2")])
    comp.__exit__(None, None, None)


def test_a_prompt_typed_at_the_computer_is_the_owners_turn(relay):
    """Seen 2026-11-16, the first day of the pause: a guest who paused was still pushed
    every response to what the owner typed at her own keyboard. 0.1.18 called such a
    turn nobody's and notified everyone; the owner at the computer is the owner."""
    comp = offline_turns(relay, owner_paused=True, guest_paused=True)
    # typed at the keyboard: an echo without a command
    comp.send_json({"type": "event", "project_id": "proj", "event": prompt_echo("typed")})
    comp.send_json({"type": "event", "project_id": "proj", "event": question("q1")})
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n1")})
    barrier(comp)
    assert pushed(relay) == [(OWNER_PUSH, "q1"), (OWNER_PUSH, "n1")]
    relay.push.calls.clear()
    # sent by the computer's own tools (aaw send, the Mac's Session Feed)
    turn(comp, comp, "c1", alert("n2"))
    assert pushed(relay) == [(OWNER_PUSH, "n2")]
    comp.__exit__(None, None, None)


def test_a_scheduled_prompt_and_an_empty_feed_notify_everyone(relay):
    comp = offline_turns(relay, owner_paused=True, guest_paused=True)
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n0")})  # no prompt in the feed yet
    barrier(comp)
    assert sorted(pushed(relay)) == sorted([(OWNER_PUSH, "n0"), (GUEST_PUSH, "n0")])
    relay.push.calls.clear()
    # scheduled from a phone: nobody is holding a phone for it when it runs
    with relay.websocket_connect("/v1/ws") as guest:
        hello_phone(guest, "gtok")
        turn(comp, guest, "g1", deliver_at=5, status="scheduled")
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n3")})
    barrier(comp)
    assert sorted(pushed(relay)) == sorted([(OWNER_PUSH, "n3"), (GUEST_PUSH, "n3")])
    comp.__exit__(None, None, None)


def test_a_phone_lists_the_sessions_it_paused(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        pair(comp, owner, guest)
        pause(owner), pause(owner, project_id="other"), pause(guest)
        owner.send_json({"type": "notify_pause", "req": "all"})
        assert recv_type(owner, "notify_pause")["paused_projects"] == ["other", "proj"]
        guest.send_json({"type": "notify_pause", "req": "all"})
        assert recv_type(guest, "notify_pause")["paused_projects"] == ["proj"]
        pause(owner, False)
        owner.send_json({"type": "notify_pause", "req": "all"})
        assert recv_type(owner, "notify_pause")["paused_projects"] == ["other"]


def test_an_answer_does_not_take_the_turn_and_resume_restores_the_pushes(relay):
    comp = offline_turns(relay, owner_paused=True)
    with relay.websocket_connect("/v1/ws") as guest:
        hello_phone(guest, "gtok")
        turn(comp, guest, "g1")
    # the owner answered her turn's question on the computer side: the echo of an answer
    comp.send_json({"type": "event", "project_id": "proj",
                    "event": prompt_echo("a1", question_id="q0", command_id="nope")})
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n1")})
    barrier(comp)
    assert pushed(relay) == [(GUEST_PUSH, "n1")]
    relay.push.calls.clear()
    with relay.websocket_connect("/v1/ws") as owner:
        hello_phone(owner, "ptok")
        assert pause(owner, None)["paused"] is True   # a read
        assert pause(owner, False)["paused"] is False
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n2")})
    barrier(comp)
    assert sorted(pushed(relay)) == sorted([(OWNER_PUSH, "n2"), (GUEST_PUSH, "n2")])
    comp.__exit__(None, None, None)


def test_a_live_paused_phone_gets_the_event_marked_quiet(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        pair(comp, owner, guest)
        pause(owner)
        turn(comp, guest, "g1", alert("n1"))
        got = recv_type(owner, "event")
        while got["event"]["id"] != "n1":
            got = recv_type(owner, "event")
        assert got["quiet"] is True
        theirs = recv_type(guest, "event")
        while theirs["event"]["id"] != "n1":
            theirs = recv_type(guest, "event")
        assert "quiet" not in theirs
        guest.send_json({"type": "ack", "project_id": "proj", "seq": theirs["seq"]})
        barrier(guest)
    assert pushed(relay) == []  # she saw it on screen; he is paused


def test_a_missed_event_is_replayed_quiet(relay):
    comp = offline_turns(relay, owner_paused=True)
    with relay.websocket_connect("/v1/ws") as guest:
        hello_phone(guest, "gtok")
        turn(comp, guest, "g1")
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n1")})
    comp.send_json({"type": "event", "project_id": "proj", "event": prompt_echo("typed")})  # his own keyboard
    comp.send_json({"type": "event", "project_id": "proj", "event": alert("n2")})
    barrier(comp)
    with relay.websocket_connect("/v1/ws") as owner:
        hello_phone(owner, "ptok")
        owner.send_json({"type": "subscribe", "project_id": "proj"})
        seen = {}
        while "n2" not in seen:
            f = recv_type(owner, "event")
            seen[f["event"]["id"]] = f.get("quiet", False)
    assert seen["n1"] is True and seen["n2"] is False
    comp.__exit__(None, None, None)


def test_a_phone_cannot_claim_how_a_command_was_sent(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        pair(comp, owner, guest)
        owner.send_json({"type": "command", "project_id": "proj", "command": command("o1", via="computer")})
        assert recv_type(comp, "command")["command"]["via"] == "phone"
        comp.send_json({"type": "command", "project_id": "proj", "command": command("c1")})
        assert "via" not in recv_type(owner, "command")["command"]


def test_a_guest_pauses_only_its_own_session_and_a_revoke_or_unlink_drops_the_pause(relay):
    with relay.websocket_connect("/v1/ws") as comp, relay.websocket_connect("/v1/ws") as owner, \
            relay.websocket_connect("/v1/ws") as guest:
        share_id = pair(comp, owner, guest)
        guest.send_json({"type": "notify_pause", "req": "x", "project_id": "other", "paused": True})
        assert recv_type(guest, "error")["message"] == "not allowed for a shared session"
        assert pause(guest)["paused"] is True
        owner.send_json({"type": "notify_pause", "req": "bad", "project_id": "proj", "paused": "yes"})
        assert recv_type(owner, "error")["req"] == "bad"
        comp.send_json({"type": "notify_pause", "req": "c", "project_id": "proj", "paused": True})
        barrier(comp)  # a computer's is ignored
        owner.send_json({"type": "revoke_share", "req": "r", "project_id": "proj", "share_id": share_id})
        recv_type(owner, "shares")
        assert pause(owner)["paused"] is True
        owner.send_json({"type": "forget_phone", "req": "f"})
        recv_type(owner, "forgotten")
    assert sqlite3.connect(relay.db).execute("SELECT COUNT(*) FROM pauses").fetchone()[0] == 0


def test_an_unknown_frame_is_refused_with_its_req(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        comp.send_json({"type": "frame_from_the_future", "req": "r9"})
        assert recv_type(comp, "error")["req"] == "r9"
