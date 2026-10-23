"""The store at rest: hashed tokens, the version guard, and the migration snapshot."""

from __future__ import annotations

import hashlib
import json
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


def test_v2_store_gets_scoped_keys_and_loses_its_plaintext(tmp_path):
    """Version 3: event and command keys scoped by computer, and the plaintext prompt,
    answer, and option list that rode next to their encrypted copies purged."""
    db = tmp_path / "relay.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE devices (token TEXT PRIMARY KEY, computer_id TEXT NOT NULL, role TEXT NOT NULL,"
        " computer_name TEXT DEFAULT '', platform TEXT DEFAULT '', push_token TEXT DEFAULT '', last_seen INTEGER DEFAULT 0);"
        "CREATE TABLE events (id TEXT PRIMARY KEY, computer_id TEXT NOT NULL, project_id TEXT NOT NULL,"
        " seq INTEGER NOT NULL, ts INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, expires_at INTEGER NOT NULL);"
        "CREATE TABLE commands (id TEXT PRIMARY KEY, computer_id TEXT NOT NULL, project_id TEXT NOT NULL,"
        " ts INTEGER NOT NULL, doc TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    con.execute("INSERT INTO meta VALUES ('store_version', '2')")
    far = 4_000_000_000
    con.execute("INSERT INTO events VALUES ('q1','c1','p',1,1,'question',?,?)",
                (json.dumps({"question": "enc", "options": ["rm -rf", "no"], "options_enc": ["e1", "e2"]}), far))
    con.execute("INSERT INTO commands VALUES ('k1','c1','p',1,?,0,?)",
                (json.dumps({"id": "k1", "type": "command", "payload": {"command": "text", "args": "secret", "args_enc": "e"}}), far))
    con.execute("INSERT INTO commands VALUES ('k2','c1','p',1,?,0,?)",
                (json.dumps({"id": "k2", "type": "answer", "payload": {"question_id": "q1", "answer": "Yes", "answer_enc": "e"}}), far))
    con.commit(); con.close()

    with TestClient(create_app(str(db))) as client, client.websocket_connect("/v1/ws") as ws:
        assert _connect(ws)["type"] == "welcome"

    con = sqlite3.connect(db)
    for table in ("events", "commands"):
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        assert "PRIMARY KEY (computer_id, id)" in sql
    assert con.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()[0] == str(STORE_VERSION)
    payload = json.loads(con.execute("SELECT payload FROM events WHERE id='q1'").fetchone()[0])
    assert "options" not in payload and payload["options_enc"] == ["e1", "e2"]
    docs = {r[0]: json.loads(r[1]) for r in con.execute("SELECT id, doc FROM commands")}
    assert docs["k1"]["payload"]["args"] == "" and docs["k1"]["payload"]["args_enc"] == "e"
    assert docs["k2"]["payload"]["answer"] == "" and docs["k2"]["payload"]["answer_enc"] == "e"
    assert (tmp_path / f"relay.sqlite.pre-v{STORE_VERSION}").exists()
    old = sqlite3.connect(tmp_path / f"relay.sqlite.pre-v{STORE_VERSION}")
    assert "secret" in old.execute("SELECT doc FROM commands WHERE id='k1'").fetchone()[0]  # the rollback copy is as it was
