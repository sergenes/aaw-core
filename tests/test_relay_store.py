"""The store at rest: hashed tokens, the version guard, and the migration snapshot."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from starlette.testclient import TestClient

from aaw_core.relay.server import STORE_VERSION, create_app


def _connect(ws, token="ctok", computer_id="c1"):
    ws.send_json({"type": "hello", "role": "computer", "token": token, "computer_id": computer_id})
    return ws.receive_json()


def test_tokens_are_stored_hashed_and_welcome_carries_proto(tmp_path):
    db = tmp_path / "relay.sqlite"
    with TestClient(create_app(str(db))) as client, client.websocket_connect("/v1/ws") as ws:
        welcome = _connect(ws)
        assert welcome["type"] == "welcome" and welcome["proto"] == 1
        ws.send_json({"type": "register_phone", "phone_token": "ptok"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    rows = {r[0] for r in sqlite3.connect(db).execute("SELECT token FROM devices")}
    assert rows == {hashlib.sha256(b"ctok").hexdigest(), hashlib.sha256(b"ptok").hexdigest()}


def test_plaintext_store_is_migrated_with_a_snapshot(tmp_path):
    db = tmp_path / "relay.sqlite"
    con = sqlite3.connect(db)  # a pre-v2 store: plaintext tokens, no meta table
    con.executescript(
        "CREATE TABLE devices (token TEXT PRIMARY KEY, computer_id TEXT NOT NULL, role TEXT NOT NULL,"
        " computer_name TEXT DEFAULT '', platform TEXT DEFAULT '', push_token TEXT DEFAULT '',"
        " last_seen INTEGER DEFAULT 0);"
        "CREATE TABLE cursors (token TEXT NOT NULL, computer_id TEXT NOT NULL, project_id TEXT NOT NULL,"
        " last_ack_seq INTEGER NOT NULL, PRIMARY KEY (token, computer_id, project_id));")
    con.execute("INSERT INTO devices VALUES ('ctok','c1','computer','box','','',7)")
    con.execute("INSERT INTO devices VALUES ('ptok','c1','phone','','ios','fcm-1',7)")
    con.execute("INSERT INTO cursors VALUES ('ptok','c1','p',5)")
    con.commit()
    con.close()

    for _ in range(2):  # the second start must change nothing
        with TestClient(create_app(str(db))) as client:
            with client.websocket_connect("/v1/ws") as ws:
                assert _connect(ws)["type"] == "welcome"  # the raw token still works
            with client.websocket_connect("/v1/ws") as ws:
                ws.send_json({"type": "hello", "role": "phone", "token": "ptok"})
                assert ws.receive_json()["type"] == "welcome"

    con = sqlite3.connect(db)
    assert {r[0] for r in con.execute("SELECT token FROM devices")} == {
        hashlib.sha256(b"ctok").hexdigest(), hashlib.sha256(b"ptok").hexdigest()}
    assert con.execute("SELECT token, last_ack_seq FROM cursors").fetchall() == [
        (hashlib.sha256(b"ptok").hexdigest(), 5)]
    assert con.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()[0] == str(STORE_VERSION)
    snapshot = tmp_path / f"relay.sqlite.pre-v{STORE_VERSION}"
    assert snapshot.exists()
    old = sqlite3.connect(snapshot)  # the snapshot still holds the pre-migration tokens
    assert {r[0] for r in old.execute("SELECT token FROM devices")} == {"ctok", "ptok"}


def test_a_newer_store_is_refused(tmp_path):
    db = tmp_path / "relay.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('store_version', '99')")
    con.commit()
    con.close()
    with pytest.raises(Exception, match="newer than this relay"), TestClient(create_app(str(db))):
        pass
