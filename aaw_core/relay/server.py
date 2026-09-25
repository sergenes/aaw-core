"""The relay server: routing, store-and-forward, and a push hook. Never plaintext.

One outbound WebSocket from each computer, one from each phone. Every payload is
ciphertext produced by the devices; the relay stores and forwards opaque documents
and reads only routing metadata (tokens, computer and project ids, sequence
numbers, timestamps).

Frames are JSON text messages with a ``type``:

  client -> relay
    hello            {role: computer|phone, token, computer_id, computer_name?, platform?, push_token?}
    register_phone   {phone_token}                              (computer) allow a phone token
    event            {project_id, event}                        (computer) new feed item
    state            {project_id, fields}                       (computer) project doc merge
    computer         {fields}                                   (computer) computer doc merge
    command          {project_id, command}                      (either)   new command / prompt / answer
    command_update   {project_id, command_id, fields}           (either)
    command_delete   {project_id, command_id}                   (either)
    subscribe        {project_id}                               (phone)    replay events after its cursor
    ack              {project_id, seq}                          (phone)    advance its cursor
    history          {req, project_id, limit?}                  (either)   retained events, oldest first
    commands         {req, project_id}                          (either)   unconsumed commands
    ping

  relay -> client
    welcome {computer_id}; event {project_id, seq, event}; state; computer; command;
    command_update; command_delete; history {req, project_id, events}; commands {req, project_id, commands};
    pong; error {message} (then the socket closes)

Trust model: a computer token is registered on first use (trust on first use) and
is bound to its computer_id forever; a phone token must have been registered by
that computer (it rides in the QR code). Tokens are routing credentials only; the
encryption key never reaches the relay.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol

import aiosqlite
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from aaw_core.transport.base import COMMAND_TTL_DAYS, EVENT_TTL_DAYS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  token TEXT PRIMARY KEY, computer_id TEXT NOT NULL, role TEXT NOT NULL,
  computer_name TEXT DEFAULT '', platform TEXT DEFAULT '', push_token TEXT DEFAULT '',
  last_seen INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS devices_computer ON devices(computer_id, role);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, computer_id TEXT NOT NULL, project_id TEXT NOT NULL,
  seq INTEGER NOT NULL, ts INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS events_stream ON events(computer_id, project_id, seq);
CREATE TABLE IF NOT EXISTS seqs (
  computer_id TEXT NOT NULL, project_id TEXT NOT NULL, last_seq INTEGER NOT NULL,
  PRIMARY KEY (computer_id, project_id));
CREATE TABLE IF NOT EXISTS cursors (
  token TEXT NOT NULL, computer_id TEXT NOT NULL, project_id TEXT NOT NULL, last_ack_seq INTEGER NOT NULL,
  PRIMARY KEY (token, computer_id, project_id));
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY, computer_id TEXT NOT NULL, project_id TEXT NOT NULL, ts INTEGER NOT NULL,
  doc TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS commands_open ON commands(computer_id, consumed);
CREATE TABLE IF NOT EXISTS projects (
  computer_id TEXT NOT NULL, project_id TEXT NOT NULL, doc TEXT NOT NULL,
  PRIMARY KEY (computer_id, project_id));
CREATE TABLE IF NOT EXISTS computers (computer_id TEXT PRIMARY KEY, doc TEXT NOT NULL);
"""


class PushSender(Protocol):
    """Wake a phone whose app is closed. Content-free: the relay hands over only an
    already-encrypted preview and routing ids."""

    async def notify(self, *, push_token: str, platform: str, computer_id: str, project_id: str,
                     event: dict) -> None: ...


class NullPushSender:
    async def notify(self, **kwargs) -> None:
        return None


@dataclass
class Conn:
    ws: WebSocket
    token: str
    role: str
    computer_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)  # one token may hold several sockets
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, frame: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(frame))


def _now_s() -> int:
    return int(time.time())


class Relay:
    def __init__(self, db_path: str, push: PushSender | None = None):
        self.db_path = db_path
        self.push: PushSender = push or NullPushSender()
        self.db: aiosqlite.Connection | None = None
        self.write_lock = asyncio.Lock()
        self.live: dict[str, dict[str, Conn]] = {}  # computer_id -> connection id -> Conn

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript(_SCHEMA)
        await self.db.commit()

    async def shutdown(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _fetchone(self, sql: str, params: tuple = ()):
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _exec(self, sql: str, params: tuple = ()) -> None:
        async with self.write_lock:
            await self.db.execute(sql, params)
            await self.db.commit()

    def _peers(self, computer_id: str, *, exclude: str | None = None, role: str | None = None) -> list[Conn]:
        return [c for t, c in self.live.get(computer_id, {}).items()
                if t != exclude and (role is None or c.role == role)]

    async def _forward(self, computer_id: str, frame: dict, *, exclude: str | None = None,
                       role: str | None = None) -> int:
        n = 0
        for conn in self._peers(computer_id, exclude=exclude, role=role):
            try:
                await conn.send(frame)
                n += 1
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass
        return n

    async def _cleanup(self) -> None:
        now = _now_s()
        await self._exec("DELETE FROM events WHERE expires_at < ?", (now,))
        await self._exec("DELETE FROM commands WHERE expires_at < ?", (now,))

    # ── connection handling ───────────────────────────────────────────────────

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        conn: Conn | None = None
        try:
            hello = json.loads(await ws.receive_text())
            conn = await self._hello(ws, hello)
            if conn is None:
                return
            self.live.setdefault(conn.computer_id, {})[conn.id] = conn
            await conn.send({"type": "welcome", "computer_id": conn.computer_id})
            if conn.role == "computer":
                await self._cleanup()
                await self._replay_commands(conn)
            while True:
                frame = json.loads(await ws.receive_text())
                await self._dispatch(conn, frame)
        except WebSocketDisconnect:
            pass
        except (ValueError, KeyError, TypeError) as e:
            await self._error(ws, f"bad frame: {e}")
        finally:
            if conn is not None:
                self.live.get(conn.computer_id, {}).pop(conn.id, None)
                await self._exec("UPDATE devices SET last_seen=? WHERE token=?", (_now_s(), conn.token))

    async def _error(self, ws: WebSocket, message: str) -> None:
        try:
            await ws.send_text(json.dumps({"type": "error", "message": message}))
            await ws.close(code=4000)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    async def _hello(self, ws: WebSocket, hello: dict) -> Conn | None:
        if hello.get("type") != "hello":
            await self._error(ws, "expected hello")
            return None
        role, token = hello.get("role"), hello.get("token")
        if role not in ("computer", "phone") or not token:
            await self._error(ws, "hello needs role and token")
            return None
        row = await self._fetchone("SELECT computer_id, role FROM devices WHERE token=?", (token,))
        if role == "computer":
            computer_id = hello.get("computer_id")
            if not computer_id:
                await self._error(ws, "computer hello needs computer_id")
                return None
            if row is None:
                # Trust on first use: the token binds to this computer_id from now on.
                await self._exec(
                    "INSERT INTO devices(token, computer_id, role, computer_name, last_seen) VALUES(?,?,?,?,?)",
                    (token, computer_id, "computer", hello.get("computer_name", ""), _now_s()))
            elif row["computer_id"] != computer_id or row["role"] != "computer":
                await self._error(ws, "token is bound to a different computer")
                return None
            else:
                await self._exec("UPDATE devices SET computer_name=?, last_seen=? WHERE token=?",
                                 (hello.get("computer_name", ""), _now_s(), token))
            return Conn(ws=ws, token=token, role="computer", computer_id=computer_id)
        if row is None or row["role"] != "phone":
            await self._error(ws, "unknown phone token; pair with the computer's QR code")
            return None
        await self._exec("UPDATE devices SET platform=?, push_token=?, last_seen=? WHERE token=?",
                         (hello.get("platform", ""), hello.get("push_token", ""), _now_s(), token))
        return Conn(ws=ws, token=token, role="phone", computer_id=row["computer_id"])

    async def _replay_commands(self, conn: Conn) -> None:
        rows = await self._fetchall(
            "SELECT project_id, doc FROM commands WHERE computer_id=? AND consumed=0 ORDER BY ts",
            (conn.computer_id,))
        for r in rows:
            await conn.send({"type": "command", "project_id": r["project_id"], "command": json.loads(r["doc"])})

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, conn: Conn, frame: dict) -> None:
        kind = frame.get("type")
        handler = {
            "ping": self._on_ping, "register_phone": self._on_register_phone, "event": self._on_event,
            "state": self._on_state, "computer": self._on_computer, "command": self._on_command,
            "command_update": self._on_command_update, "command_delete": self._on_command_delete,
            "subscribe": self._on_subscribe, "ack": self._on_ack, "history": self._on_history,
            "commands": self._on_commands, "project": self._on_project, "clear_events": self._on_clear_events,
        }.get(kind)
        if handler is None:
            await conn.send({"type": "error", "message": f"unknown frame type {kind!r}"})
            return
        await handler(conn, frame)

    async def _on_ping(self, conn: Conn, frame: dict) -> None:
        await conn.send({"type": "pong"})

    async def _on_register_phone(self, conn: Conn, frame: dict) -> None:
        if conn.role != "computer":
            return
        token = frame["phone_token"]
        await self._exec(
            "INSERT OR REPLACE INTO devices(token, computer_id, role, last_seen) VALUES(?,?,?,?)",
            (token, conn.computer_id, "phone", 0))

    async def _on_event(self, conn: Conn, frame: dict) -> None:
        if conn.role != "computer":
            return
        project_id, event = frame["project_id"], frame["event"]
        async with self.write_lock:
            row = await self._fetchone("SELECT last_seq FROM seqs WHERE computer_id=? AND project_id=?",
                                       (conn.computer_id, project_id))
            seq = (row["last_seq"] if row else 0) + 1
            await self.db.execute(
                "INSERT OR REPLACE INTO seqs(computer_id, project_id, last_seq) VALUES(?,?,?)",
                (conn.computer_id, project_id, seq))
            await self.db.execute(
                "INSERT OR REPLACE INTO events(id, computer_id, project_id, seq, ts, type, payload, expires_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (event["id"], conn.computer_id, project_id, seq, int(event.get("ts", 0)), event.get("type", ""),
                 json.dumps(event.get("payload") or {}), _now_s() + EVENT_TTL_DAYS * 86400))
            await self.db.commit()
        delivered = await self._forward(conn.computer_id, {"type": "event", "project_id": project_id,
                                                           "seq": seq, "event": event}, role="phone")
        if delivered == 0:
            await self._push(conn.computer_id, project_id, event)

    async def _push(self, computer_id: str, project_id: str, event: dict) -> None:
        rows = await self._fetchall(
            "SELECT push_token, platform FROM devices WHERE computer_id=? AND role='phone' AND push_token<>''",
            (computer_id,))
        for r in rows:
            try:
                await self.push.notify(push_token=r["push_token"], platform=r["platform"],
                                       computer_id=computer_id, project_id=project_id, event=event)
            except Exception as e:  # noqa: BLE001 - a push failure must never break routing
                print(f"[relay] push failed: {e}", file=sys.stderr, flush=True)

    async def _merge_doc(self, table: str, key_sql: str, key: tuple, fields: dict, insert_sql: str) -> dict:
        row = await self._fetchone(f"SELECT doc FROM {table} WHERE {key_sql}", key)
        doc = json.loads(row["doc"]) if row else {}
        doc.update(fields)
        await self._exec(insert_sql, (*key, json.dumps(doc)))
        return doc

    async def _on_state(self, conn: Conn, frame: dict) -> None:
        if conn.role != "computer":
            return
        project_id, fields = frame["project_id"], frame.get("fields") or {}
        await self._merge_doc("projects", "computer_id=? AND project_id=?", (conn.computer_id, project_id), fields,
                              "INSERT OR REPLACE INTO projects(computer_id, project_id, doc) VALUES(?,?,?)")
        await self._forward(conn.computer_id, {"type": "state", "project_id": project_id, "fields": fields},
                            role="phone")

    async def _on_computer(self, conn: Conn, frame: dict) -> None:
        if conn.role != "computer":
            return
        fields = frame.get("fields") or {}
        await self._merge_doc("computers", "computer_id=?", (conn.computer_id,), fields,
                              "INSERT OR REPLACE INTO computers(computer_id, doc) VALUES(?,?)")
        await self._forward(conn.computer_id, {"type": "computer", "fields": fields}, role="phone")

    async def _on_command(self, conn: Conn, frame: dict) -> None:
        project_id, doc = frame["project_id"], frame["command"]
        await self._exec(
            "INSERT OR REPLACE INTO commands(id, computer_id, project_id, ts, doc, consumed, expires_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (doc["id"], conn.computer_id, project_id, int(doc.get("ts", 0)), json.dumps(doc),
             1 if doc.get("consumed") else 0, _now_s() + COMMAND_TTL_DAYS * 86400))
        await self._forward(conn.computer_id, {"type": "command", "project_id": project_id, "command": doc},
                            exclude=conn.id)

    async def _on_command_update(self, conn: Conn, frame: dict) -> None:
        project_id, command_id, fields = frame["project_id"], frame["command_id"], frame.get("fields") or {}
        row = await self._fetchone("SELECT doc FROM commands WHERE id=? AND computer_id=?",
                                   (command_id, conn.computer_id))
        if row is None:
            return
        doc = json.loads(row["doc"])
        doc.update(fields)
        await self._exec("UPDATE commands SET doc=?, consumed=? WHERE id=?",
                         (json.dumps(doc), 1 if doc.get("consumed") else 0, command_id))
        await self._forward(conn.computer_id, {"type": "command_update", "project_id": project_id,
                                               "command_id": command_id, "fields": fields}, exclude=conn.id)

    async def _on_command_delete(self, conn: Conn, frame: dict) -> None:
        project_id, command_id = frame["project_id"], frame["command_id"]
        await self._exec("DELETE FROM commands WHERE id=? AND computer_id=?", (command_id, conn.computer_id))
        await self._forward(conn.computer_id, {"type": "command_delete", "project_id": project_id,
                                               "command_id": command_id}, exclude=conn.id)

    async def _on_subscribe(self, conn: Conn, frame: dict) -> None:
        if conn.role != "phone":
            return
        project_id = frame["project_id"]
        cur = await self._fetchone(
            "SELECT last_ack_seq FROM cursors WHERE token=? AND computer_id=? AND project_id=?",
            (conn.token, conn.computer_id, project_id))
        after = cur["last_ack_seq"] if cur else 0
        rows = await self._fetchall(
            "SELECT id, seq, ts, type, payload FROM events WHERE computer_id=? AND project_id=? AND seq>? ORDER BY seq",
            (conn.computer_id, project_id, after))
        for r in rows:
            await conn.send({"type": "event", "project_id": project_id, "seq": r["seq"],
                             "event": {"id": r["id"], "type": r["type"], "ts": r["ts"],
                                       "payload": json.loads(r["payload"])}})

    async def _on_ack(self, conn: Conn, frame: dict) -> None:
        if conn.role != "phone":
            return
        await self._exec(
            "INSERT OR REPLACE INTO cursors(token, computer_id, project_id, last_ack_seq) VALUES(?,?,?,?)",
            (conn.token, conn.computer_id, frame["project_id"], int(frame["seq"])))

    async def _on_history(self, conn: Conn, frame: dict) -> None:
        project_id, limit = frame["project_id"], int(frame.get("limit") or 0)
        sql = "SELECT id, ts, type, payload FROM events WHERE computer_id=? AND project_id=? ORDER BY seq DESC"
        params: tuple = (conn.computer_id, project_id)
        if limit > 0:
            sql += " LIMIT ?"
            params = (*params, limit)
        rows = list(await self._fetchall(sql, params))
        rows.reverse()
        await conn.send({"type": "history", "req": frame.get("req"), "project_id": project_id,
                         "events": [{"id": r["id"], "type": r["type"], "ts": r["ts"],
                                     "payload": json.loads(r["payload"])} for r in rows]})

    async def _on_commands(self, conn: Conn, frame: dict) -> None:
        project_id = frame["project_id"]
        rows = await self._fetchall(
            "SELECT doc FROM commands WHERE computer_id=? AND project_id=? AND consumed=0 ORDER BY ts",
            (conn.computer_id, project_id))
        await conn.send({"type": "commands", "req": frame.get("req"), "project_id": project_id,
                         "commands": [json.loads(r["doc"]) for r in rows]})

    async def _on_project(self, conn: Conn, frame: dict) -> None:
        """The merged project document (status, auto_approve, pending_question_id, ...)."""
        project_id = frame["project_id"]
        row = await self._fetchone("SELECT doc FROM projects WHERE computer_id=? AND project_id=?",
                                   (conn.computer_id, project_id))
        await conn.send({"type": "project", "req": frame.get("req"), "project_id": project_id,
                         "fields": json.loads(row["doc"]) if row else {}})

    async def _on_clear_events(self, conn: Conn, frame: dict) -> None:
        """Wipe a project's retained feed (the user ran /clear); sequence numbers start over."""
        if conn.role != "computer":
            return
        project_id = frame["project_id"]
        async with self.write_lock:
            for table in ("events", "cursors", "seqs"):
                await self.db.execute(f"DELETE FROM {table} WHERE computer_id=? AND project_id=?",
                                      (conn.computer_id, project_id))
            await self.db.commit()
        await self._forward(conn.computer_id, {"type": "clear_events", "project_id": project_id}, role="phone")


def create_app(db_path: str, push: PushSender | None = None) -> Starlette:
    relay = Relay(db_path, push)

    async def healthz(request):
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await relay.startup()
        try:
            yield
        finally:
            await relay.shutdown()

    app = Starlette(
        routes=[Route("/healthz", healthz), WebSocketRoute("/v1/ws", relay.handle)],
        lifespan=lifespan,
    )
    app.state.relay = relay
    return app
