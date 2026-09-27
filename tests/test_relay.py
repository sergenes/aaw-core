"""The relay server (via Starlette's test client) and a real RelayTransport <-> server
integration over uvicorn, with the phone simulated by the sync websocket client."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from starlette.testclient import TestClient
from websockets.sync.client import connect as ws_connect

from aaw_core.encryption import decrypt, generate_key_b64, is_encrypted
from aaw_core.relay.server import StalePushToken, create_app
from aaw_core.transport.base import make_command
from aaw_core.transport.relay import RelayTransport, new_token

KEY = generate_key_b64()


class RecordingPush:
    def __init__(self):
        self.calls: list[dict] = []

    async def notify(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def relay(tmp_path):
    push = RecordingPush()
    app = create_app(str(tmp_path / "relay.sqlite"), push=push)
    with TestClient(app) as client:
        client.push = push
        yield client


def hello_computer(ws, token="ctok", computer_id="c1"):
    ws.send_json({"type": "hello", "role": "computer", "token": token, "computer_id": computer_id,
                  "computer_name": "box"})
    return ws.receive_json()


def hello_phone(ws, token, **extra):
    ws.send_json({"type": "hello", "role": "phone", "token": token, **extra})
    return ws.receive_json()


def register_phone(comp, token="ptok"):
    """Register a phone token and wait until the relay has processed it (a ping/pong barrier),
    so a phone connecting right after cannot race ahead of the registration."""
    comp.send_json({"type": "register_phone", "phone_token": token})
    comp.send_json({"type": "ping"})
    assert comp.receive_json() == {"type": "pong"}


def event(event_id="e1", content="hi"):
    return {"id": event_id, "type": "message", "ts": 1, "payload": {"role": "user", "content": content}}


def alert(event_id="n1"):
    """An event that pushes (a "Response ready" notification); a message never does."""
    return {"id": event_id, "type": "notification", "ts": 1, "payload": {"level": "success", "message": "enc"}}


# ── server unit tests ───────────────────────────────────────────────────────


def test_computer_token_is_trust_on_first_use_then_bound(relay):
    with relay.websocket_connect("/v1/ws") as ws:
        assert hello_computer(ws) == {"type": "welcome", "computer_id": "c1"}
    with relay.websocket_connect("/v1/ws") as ws:
        assert hello_computer(ws, computer_id="c1")["type"] == "welcome"  # same binding: fine
    with relay.websocket_connect("/v1/ws") as ws:
        err = hello_computer(ws, computer_id="other")  # same token, other computer: refused
        assert err["type"] == "error" and "bound" in err["message"]


def test_phone_needs_a_registered_token(relay):
    with relay.websocket_connect("/v1/ws") as ws:
        err = hello_phone(ws, "nope")
        assert err["type"] == "error" and "unknown phone token" in err["message"]
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}  # registration is ordered before the pong
    with relay.websocket_connect("/v1/ws") as phone:
        assert hello_phone(phone, "ptok") == {"type": "welcome", "computer_id": "c1"}


def test_events_forward_live_with_sequence_numbers_and_replay_from_cursor(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            comp.send_json({"type": "event", "project_id": "p", "event": event("e1", "one")})
            got = phone.receive_json()
            assert got["type"] == "event" and got["seq"] == 1 and got["event"]["payload"]["content"] == "one"
            phone.send_json({"type": "ack", "project_id": "p", "seq": 1})
            comp.send_json({"type": "ping"})
            assert comp.receive_json() == {"type": "pong"}
        # phone gone: the next event is stored, not delivered, and the push hook fires
        comp.send_json({"type": "event", "project_id": "p", "event": event("e2", "two")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
        # phone comes back and subscribes: only what it has not acked is replayed
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            phone.send_json({"type": "subscribe", "project_id": "p"})
            got = phone.receive_json()
            assert got["seq"] == 2 and got["event"]["id"] == "e2"
            # history returns everything retained, oldest first
            phone.send_json({"type": "history", "req": "r1", "project_id": "p"})
            hist = phone.receive_json()
            assert hist["type"] == "history" and hist["req"] == "r1"
            assert [e["id"] for e in hist["events"]] == ["e1", "e2"]
            phone.send_json({"type": "history", "req": "r2", "project_id": "p", "limit": 1})
            assert [e["id"] for e in phone.receive_json()["events"]] == ["e2"]


def test_push_hook_fires_only_when_no_phone_is_live(relay, monkeypatch):
    monkeypatch.setattr("aaw_core.relay.server.ACK_WAIT_S", 0.3)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok", platform="ios", push_token="apns-123")
            comp.send_json({"type": "event", "project_id": "p", "event": alert("e1")})
            got = phone.receive_json()
            phone.send_json({"type": "ack", "project_id": "p", "seq": got["seq"]})
            time.sleep(0.6)
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
        assert relay.push.calls == []  # a live phone got it and acked it
        comp.send_json({"type": "event", "project_id": "p", "event": alert("e2")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
    assert len(relay.push.calls) == 1
    call = relay.push.calls[0]
    assert call["push_token"] == "apns-123" and call["platform"] == "ios"
    assert call["computer_id"] == "c1" and call["project_id"] == "p" and call["event"]["id"] == "e2"


def test_a_phone_on_screen_is_not_pushed_through_its_older_pairing(relay, monkeypatch):
    """A phone that scanned twice holds two tokens with one push token; an ack on either
    one means the phone has the event on screen."""
    monkeypatch.setattr("aaw_core.relay.server.ACK_WAIT_S", 0.3)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "old-scan")
        register_phone(comp, "new-scan")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "old-scan", platform="ios", push_token="apns-same")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "new-scan", platform="ios", push_token="apns-same")
            comp.send_json({"type": "event", "project_id": "p", "event": alert("n1")})
            got = phone.receive_json()
            phone.send_json({"type": "ack", "project_id": "p", "seq": got["seq"]})
            time.sleep(0.6)
    assert relay.push.calls == []


def test_a_message_never_pushes(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok", platform="ios", push_token="apns-123")
        comp.send_json({"type": "event", "project_id": "p", "event": event("e1")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
    assert relay.push.calls == []


def test_a_hello_without_a_push_token_keeps_the_stored_one(relay, monkeypatch):
    """An app connects before its push token is ready; the phone must stay reachable."""
    monkeypatch.setattr("aaw_core.relay.server.ACK_WAIT_S", 0.3)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok", platform="ios", push_token="apns-123")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok", platform="ios")  # a relaunch, the push token not ready yet
        comp.send_json({"type": "event", "project_id": "p", "event": alert("e1")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
    assert [c["push_token"] for c in relay.push.calls] == ["apns-123"]


def test_commands_replay_to_computer_until_consumed(relay):
    cmd = make_command("run the tests", KEY)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            phone.send_json({"type": "command", "project_id": "p", "command": cmd})
            got = comp.receive_json()
            assert got["type"] == "command" and got["command"]["id"] == cmd["id"]
    # computer reconnects: the unconsumed command is replayed right after welcome
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        assert comp.receive_json()["command"]["id"] == cmd["id"]
        comp.send_json({"type": "command_update", "project_id": "p", "command_id": cmd["id"],
                        "fields": {"consumed": True, "status": "done"}})
        comp.send_json({"type": "commands", "req": "r", "project_id": "p"})
        assert comp.receive_json()["commands"] == []
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}  # nothing replayed before the pong


def test_command_delete_and_update_fan_out_to_the_other_side(relay):
    cmd = make_command("later", KEY, deliver_at=4_000_000_000_000)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            comp.send_json({"type": "command", "project_id": "p", "command": cmd})  # host-side schedule
            assert phone.receive_json()["command"]["id"] == cmd["id"]  # the phone's queue sees it
            comp.send_json({"type": "command_update", "project_id": "p", "command_id": cmd["id"],
                            "fields": {"deliver_at": 4_000_000_001_000}})
            upd = phone.receive_json()
            assert upd["type"] == "command_update" and upd["fields"]["deliver_at"] == 4_000_000_001_000
            comp.send_json({"type": "command_delete", "project_id": "p", "command_id": cmd["id"]})
            assert phone.receive_json() == {"type": "command_delete", "project_id": "p", "command_id": cmd["id"]}
            phone.send_json({"type": "commands", "req": "r", "project_id": "p"})
            assert phone.receive_json()["commands"] == []


def test_state_and_computer_docs_merge_and_forward(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            comp.send_json({"type": "state", "project_id": "p", "fields": {"status": "running"}})
            assert phone.receive_json() == {"type": "state", "project_id": "p", "fields": {"status": "running"}}
            comp.send_json({"type": "computer", "fields": {"name": "box", "status": "online"}})
            assert phone.receive_json()["fields"]["name"] == "box"
    relay_obj = relay.app.state.relay

    async def read():
        row = await relay_obj._fetchone("SELECT doc FROM projects WHERE computer_id='c1' AND project_id='p'")
        return json.loads(row["doc"])

    assert relay.portal.call(read) == {"status": "running"}


def test_healthz(relay):
    assert relay.get("/healthz").json() == {"ok": True}


def test_several_sockets_may_share_one_computer_token(relay):
    """A hook is a short-lived process that opens its own socket with the daemon's token.
    Both must stay routed: a phone command reaches every live socket of that computer,
    and closing the hook's socket must not evict the daemon's."""
    cmd = make_command("from the phone", KEY)
    with relay.websocket_connect("/v1/ws") as daemon:
        hello_computer(daemon)
        register_phone(daemon, "ptok")
        with relay.websocket_connect("/v1/ws") as hook:
            assert hello_computer(hook)["type"] == "welcome"  # same token, second socket
            with relay.websocket_connect("/v1/ws") as phone:
                hello_phone(phone, "ptok")
                phone.send_json({"type": "command", "project_id": "p", "command": cmd})
                assert daemon.receive_json()["command"]["id"] == cmd["id"]
                assert hook.receive_json()["command"]["id"] == cmd["id"]
        # the hook is gone; the daemon is still the computer's live socket
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            cmd2 = make_command("again", KEY)
            phone.send_json({"type": "command", "project_id": "p", "command": cmd2})
            assert daemon.receive_json()["command"]["id"] == cmd2["id"]


def test_project_doc_read_back_and_clear_events(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        comp.send_json({"type": "project", "req": "r0", "project_id": "p"})
        assert comp.receive_json() == {"type": "project", "req": "r0", "project_id": "p", "fields": {}}
        comp.send_json({"type": "state", "project_id": "p", "fields": {"auto_approve": True, "status": "running"}})
        comp.send_json({"type": "project", "req": "r1", "project_id": "p"})
        assert comp.receive_json()["fields"] == {"auto_approve": True, "status": "running"}
        comp.send_json({"type": "event", "project_id": "p", "event": event("e1")})
        comp.send_json({"type": "clear_events", "project_id": "p"})
        comp.send_json({"type": "history", "req": "r2", "project_id": "p"})
        assert comp.receive_json()["events"] == []
        comp.send_json({"type": "event", "project_id": "p", "event": event("e2")})
        comp.send_json({"type": "history", "req": "r3", "project_id": "p"})
        assert [e["id"] for e in comp.receive_json()["events"]] == ["e2"]


# ── integration: RelayTransport <-> uvicorn ─────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_relay(tmp_path):
    port = _free_port()
    app = create_app(str(tmp_path / "relay.sqlite"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"ws://127.0.0.1:{port}/v1/ws"
    server.should_exit = True
    thread.join(timeout=5)


def _phone_connect(url: str, token: str):
    """The phone: connect + hello, retrying briefly until the computer's registration lands."""
    deadline = time.time() + 5
    while True:
        ws = ws_connect(url, legacy=True)  # held open across the test, not used as a context manager
        ws.send(json.dumps({"type": "hello", "role": "phone", "token": token}))
        reply = json.loads(ws.recv(timeout=5))
        if reply.get("type") == "welcome":
            return ws
        ws.close()
        assert time.time() < deadline, reply
        time.sleep(0.1)


def _recv_until(ws, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
        if predicate(frame):
            return frame
    raise AssertionError("expected frame not received")


def _poll_until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_transport_end_to_end(live_relay, tmp_path):
    rt = RelayTransport(relay_url=live_relay, token=new_token(), computer_id="mac-1", project_id="proj",
                        sessions_dir=tmp_path / "sessions", computer_name="box", enc_key=KEY).start()
    try:
        assert rt.wait_connected(10)
        phone_token = rt.register_phone_token()
        phone = _phone_connect(live_relay, phone_token)

        # an event: plaintext in the local log, ciphertext over the wire, decrypted by the phone
        rt.init_local_log()
        eid = rt.write_event("message", {"role": "user", "content": "hello from the host"})
        assert rt._log.read()[0]["content"] == "hello from the host"
        phone.send(json.dumps({"type": "subscribe", "project_id": "proj"}))
        got = _recv_until(phone, lambda f: f.get("type") == "event")
        assert got["seq"] == 1 and got["event"]["id"] == eid
        assert is_encrypted(got["event"]["payload"]["content"])
        assert decrypt(got["event"]["payload"]["content"], KEY) == "hello from the host"
        phone.send(json.dumps({"type": "ack", "project_id": "proj", "seq": 1}))

        # the retained history comes back decrypted
        assert [e["content"] for e in rt.read_events()] == ["hello from the host"]

        # a prompt from the phone reaches poll_commands decrypted, and consuming it fans out
        cmd = make_command("run the tests", KEY, source="phone")
        phone.send(json.dumps({"type": "command", "project_id": "proj", "command": cmd}))
        delivered = _poll_until(rt.poll_commands)
        assert delivered[0]["id"] == cmd["id"] and delivered[0]["payload"]["args"] == "run the tests"
        rt.mark_command_done(cmd["id"])
        upd = _recv_until(phone, lambda f: f.get("type") == "command_update" and f["command_id"] == cmd["id"])
        assert upd["fields"]["consumed"] is True and upd["fields"]["status"] == "done"
        assert rt.poll_commands() == []

        # a scheduled prompt stays queued, shows in the queue, and updates the project state
        sched_id = rt.write_command("post the release", deliver_at=int(time.time() * 1000) + 3_600_000)
        assert rt.poll_commands() == []
        assert [s["text"] for s in rt.list_scheduled()] == ["post the release"]
        state = _recv_until(phone, lambda f: f.get("type") == "state" and "scheduled_count" in f["fields"])
        assert state["fields"]["scheduled_count"] == 1
        rt.cancel_command(sched_id)
        _recv_until(phone, lambda f: f.get("type") == "command_delete" and f["command_id"] == sched_id)
        assert rt.list_scheduled() == []

        # a question goes out encrypted; the phone's encrypted answer comes back as plaintext
        rt.send_question("proj", "claude", "Allow rm -rf build?", options=["Yes", "No"])
        q = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "question")
        assert decrypt(q["event"]["payload"]["question"], KEY) == "Allow rm -rf build?"
        from aaw_core.encryption import encrypt

        phone.send(json.dumps({"type": "command", "project_id": "proj", "command": {
            "id": "ans-1", "type": "answer", "ts": int(time.time() * 1000), "consumed": False,
            "payload": {"question_id": q["event"]["id"], "answer_enc": encrypt("Yes", KEY)}}}))
        assert _poll_until(lambda: rt.poll_perm_answer_once(q["event"]["id"])) == "Yes"
        # the computer echoes the answer into the feed, since the phone cannot write events
        echoed = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "message"
                             and decrypt(f["event"]["payload"]["content"], KEY) == "Yes")
        assert echoed["event"]["payload"]["role"] == "user"
        phone.close()
    finally:
        rt.stop()


def test_projects_listing_and_request_response_routing(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp)
        comp.send_json({"type": "state", "project_id": "a", "fields": {"status": "running"}})
        comp.send_json({"type": "state", "project_id": "b", "fields": {"status": "stopped"}})
        comp.send_json({"type": "projects", "req": "r1"})
        got = comp.receive_json()
        assert got["type"] == "projects" and got["req"] == "r1"
        assert got["projects"] == {"a": {"status": "running"}, "b": {"status": "stopped"}}
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            # the phone asks, the computer answers, the reply lands on the phone
            phone.send_json({"type": "request", "req": "q1", "kind": "fs_browse", "payload": {"path_enc": "x"}})
            req = comp.receive_json()
            assert req == {"type": "request", "req": "q1", "kind": "fs_browse", "payload": {"path_enc": "x"}}
            comp.send_json({"type": "response", "req": "q1", "kind": "fs_browse", "payload": {"error": ""}})
            assert phone.receive_json() == {"type": "response", "req": "q1", "kind": "fs_browse",
                                            "payload": {"error": ""}}
            # a computer cannot send requests, and a phone cannot answer them (both ignored)
            comp.send_json({"type": "request", "req": "q2", "kind": "fs_browse", "payload": {}})
            phone.send_json({"type": "response", "req": "q2", "kind": "fs_browse", "payload": {}})
            comp.send_json({"type": "ping"})
            assert comp.receive_json() == {"type": "pong"}
    # no computer socket: the relay answers offline at once
    with relay.websocket_connect("/v1/ws") as phone:
        hello_phone(phone, "ptok")
        phone.send_json({"type": "request", "req": "q3", "kind": "start_session", "payload": {"project_id": "b"}})
        assert phone.receive_json() == {"type": "response", "req": "q3", "kind": "start_session",
                                        "payload": {"error": "offline"}}


def test_transport_serves_requests_and_lists_projects(live_relay, tmp_path):
    rt = RelayTransport(relay_url=live_relay, token=new_token(), computer_id="mac-2", project_id="_supervisor",
                        sessions_dir=tmp_path / "sessions", computer_name="box", enc_key=KEY).start()
    try:
        assert rt.wait_connected(10)
        phone = _phone_connect(live_relay, rt.register_phone_token())
        rt.set_project_fields("proj", {"status": "stopped", "agent": "codex"})
        assert _poll_until(lambda: rt.list_projects().get("proj")) == {"status": "stopped", "agent": "codex"}
        assert rt.list_commands("proj") == []
        phone.send(json.dumps({"type": "request", "req": "r9", "kind": "start_session",
                               "payload": {"project_id": "proj"}}))
        frame = rt.requests.get(timeout=5)
        assert frame["kind"] == "start_session" and frame["payload"] == {"project_id": "proj"}
        rt.respond(frame, {"result": "started", "session_id": "proj"})
        reply = _recv_until(phone, lambda f: f.get("type") == "response")
        assert reply["req"] == "r9" and reply["payload"] == {"result": "started", "session_id": "proj"}
        phone.close()
    finally:
        rt.stop()


def test_auto_approve_skips_permission_pushes_but_not_choices(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp)
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok", platform="ios", push_token="apns-1")
        comp.send_json({"type": "state", "project_id": "p", "fields": {"auto_approve": True}})
        comp.send_json({"type": "event", "project_id": "p", "event": {
            "id": "q1", "type": "question", "ts": 1, "payload": {"question": "x", "kind": "permission"}}})
        comp.send_json({"type": "event", "project_id": "p", "event": {
            "id": "q2", "type": "question", "ts": 2, "payload": {"question": "x", "kind": "choice"}}})
        comp.send_json({"type": "event", "project_id": "p", "event": {
            "id": "n1", "type": "notification", "ts": 3, "payload": {"message": "x", "level": "error"}}})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
    assert [c["event"]["id"] for c in relay.push.calls] == ["q2", "n1"]


def test_a_stale_push_token_is_forgotten(tmp_path):
    class StalePush:
        def __init__(self):
            self.calls = 0

        async def notify(self, **kwargs):
            self.calls += 1
            raise StalePushToken(kwargs["push_token"])

    push = StalePush()
    with TestClient(create_app(str(tmp_path / "r.sqlite"), push=push)) as client, \
         client.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp)
        with client.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok", platform="ios", push_token="apns-old")
        comp.send_json({"type": "event", "project_id": "p", "event": alert("e1")})
        comp.send_json({"type": "event", "project_id": "p", "event": alert("e2")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
    assert push.calls == 1  # the second event found no token left to push to


def test_phone_reads_the_computer_doc_and_sets_only_its_own_project_fields(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp)
        comp.send_json({"type": "computer", "fields": {"name": "box", "platform": "macos"}})
        comp.send_json({"type": "state", "project_id": "p", "fields": {"status": "running"}})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            phone.send_json({"type": "computer", "req": "r1"})
            assert phone.receive_json() == {"type": "computer", "req": "r1",
                                            "fields": {"name": "box", "platform": "macos"}}
            # auto_approve lands on the doc; status is the computer's and is dropped
            phone.send_json({"type": "state", "project_id": "p", "fields": {"auto_approve": True, "status": "stopped"}})
            phone.send_json({"type": "project", "req": "r2", "project_id": "p"})
            assert phone.receive_json()["fields"] == {"status": "running", "auto_approve": True}
            # a merge with nothing the phone may set is ignored entirely
            phone.send_json({"type": "state", "project_id": "p", "fields": {"status": "stopped"}})
            phone.send_json({"type": "project", "req": "r3", "project_id": "p"})
            assert phone.receive_json()["fields"]["status"] == "running"


def test_project_delete_forgets_everything_and_tells_the_other_side(relay):
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp)
        comp.send_json({"type": "state", "project_id": "p", "fields": {"status": "stopped"}})
        comp.send_json({"type": "event", "project_id": "p", "event": event("e1")})
        comp.send_json({"type": "command", "project_id": "p", "command": make_command("x", None)})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}  # everything above is stored before the phone joins
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok")
            phone.send_json({"type": "project_delete", "project_id": "p"})
            assert comp.receive_json() == {"type": "project_delete", "project_id": "p"}
            phone.send_json({"type": "projects", "req": "r1"})
            assert phone.receive_json()["projects"] == {}
            phone.send_json({"type": "history", "req": "r2", "project_id": "p"})
            assert phone.receive_json()["events"] == []
            phone.send_json({"type": "commands", "req": "r3", "project_id": "p"})
            assert phone.receive_json()["commands"] == []


def test_a_live_socket_that_never_acks_is_pushed_anyway(relay, monkeypatch):
    """The app was killed from the switcher (no clean close) or the network died under it:
    the socket still accepts a send, so only the missing ack tells the relay to push."""
    monkeypatch.setattr("aaw_core.relay.server.ACK_WAIT_S", 0.3)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok")
        register_phone(comp, "ptok2")
        with relay.websocket_connect("/v1/ws") as silent, relay.websocket_connect("/v1/ws") as awake:
            hello_phone(silent, "ptok", platform="ios", push_token="apns-dead")
            hello_phone(awake, "ptok2", platform="android", push_token="fcm-live")
            comp.send_json({"type": "event", "project_id": "p", "event": alert("e1")})
            silent.receive_json()  # delivered at the socket level, never acked
            got = awake.receive_json()
            awake.send_json({"type": "ack", "project_id": "p", "seq": got["seq"]})
            time.sleep(0.8)
            assert [c["push_token"] for c in relay.push.calls] == ["apns-dead"]


def test_forget_phone_drops_the_token_and_one_push_per_push_token(relay, monkeypatch):
    monkeypatch.setattr("aaw_core.relay.server.ACK_WAIT_S", 0.2)
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "scan1")
        register_phone(comp, "scan2")  # a re-scan on the same phone: two tokens, one push token
        for token in ("scan1", "scan2"):
            with relay.websocket_connect("/v1/ws") as phone:
                hello_phone(phone, token, platform="ios", push_token="apns-same")
        comp.send_json({"type": "event", "project_id": "p", "event": alert("e1")})
        comp.send_json({"type": "ping"})
        assert comp.receive_json() == {"type": "pong"}
        assert [c["push_token"] for c in relay.push.calls] == ["apns-same"]  # not twice

        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "scan2")
            phone.send_json({"type": "forget_phone", "req": "r1"})
            assert phone.receive_json() == {"type": "forgotten", "req": "r1"}
        with relay.websocket_connect("/v1/ws") as phone:
            phone.send_json({"type": "hello", "role": "phone", "token": "scan2"})
            assert "unknown phone token" in phone.receive_json()["message"]


def test_pairing_events_are_logged_one_line_each(relay, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="aaw_core.relay")
    with relay.websocket_connect("/v1/ws") as comp:
        hello_computer(comp)
        register_phone(comp, "ptok-logged")
        with relay.websocket_connect("/v1/ws") as phone:
            hello_phone(phone, "ptok-logged", platform="android", push_token="fcm-1")
            phone.send_json({"type": "forget_phone"})
            assert phone.receive_json()["type"] == "forgotten"
    messages = [r.getMessage() for r in caplog.records if r.name == "aaw_core.relay"]
    assert any(m.startswith("computer c1 (") and "registered" in m for m in messages)
    assert "computer c1 registered phone token ptok-log (QR shown)" in messages
    assert "phone ptok-log (android, push yes) connected to computer c1" in messages
    assert "phone ptok-log forgotten by its own request (unlink), computer c1" in messages
    assert not any("ptok-logged" in m for m in messages)  # never the whole token
