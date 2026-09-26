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
    state            {project_id, fields}                       (either)   project doc merge; a phone may
                                                                           set only auto_approve, pending_message
    computer         {fields}                                   (computer) computer doc merge
    computer         {req}                                      (phone)    read the computer doc
    project_delete   {project_id}                               (either)   forget a session entirely
    forget_phone     {req}                                      (phone)    unlink: drop this token and its push token
    command          {project_id, command}                      (either)   new command / prompt / answer
    command_update   {project_id, command_id, fields}           (either)
    command_delete   {project_id, command_id}                   (either)
    subscribe        {project_id}                               (phone)    replay events after its cursor
    ack              {project_id, seq}                          (phone)    advance its cursor
    history          {req, project_id, limit?}                  (either)   retained events, oldest first
    commands         {req, project_id}                          (either)   unconsumed commands
    project          {req, project_id}                          (either)   the merged project doc
    projects         {req}                                      (either)   every project doc of the computer
    clear_events     {project_id}                               (computer) wipe a project's feed
    request          {req, kind, payload}                       (phone)    ask the computer (start a
                                                                           session, browse a folder, ...)
    response         {req, kind, payload}                       (computer) the answer to a request
    ping

  relay -> client
    welcome {computer_id}; event {project_id, seq, event}; state; computer; command;
    command_update; command_delete; history {req, project_id, events}; commands {req, project_id, commands};
    project {req, project_id, fields}; projects {req, projects}; computer {req, fields}; clear_events {project_id};
    project_delete {project_id}; forgotten {req};
    request {req, kind, payload} (to the computer); response {req, kind, payload} (to the phones;
    the relay itself answers {error: "offline"} when no computer socket is live);
    pong; error {message} (then the socket closes)

A request is live-only: the computer must be connected to answer it, and the
phone learns at once when it is not. Everything else is stored and forwarded.

Trust model: a computer token is registered on first use (trust on first use) and
is bound to its computer_id forever; a phone token must have been registered by
that computer (it rides in the QR code). Tokens are routing credentials only; the
encryption key never reaches the relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

log = logging.getLogger("aaw_core.relay")


def _short(token: str) -> str:
    """The first characters of a token, enough to match rows in the store, never the whole."""
    return token[:8]


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


# The project fields a phone may set; everything else on the document is the computer's.
PHONE_STATE_FIELDS = {"auto_approve", "pending_message"}

# How long a live phone socket gets to ack an event before the phone is pushed anyway. A
# socket whose app was killed from the switcher (no clean close) or whose network is gone
# still accepts a send at the OS level; only the ack proves the phone saw the event.
ACK_WAIT_S = 4.0


class StalePushToken(Exception):
    """The push service no longer knows this token; the relay forgets it."""

    def __init__(self, push_token: str):
        super().__init__(push_token)
        self.push_token = push_token


class PushSender(Protocol):
    """Wake a phone whose app is closed. Content-free: the relay hands over only an
    already-encrypted preview and routing ids. Raises ``StalePushToken`` for a token
    the push service rejects for good; ``aaw_core.relay.push_fcm`` is the FCM one."""

    async def notify(self, *, push_token: str, platform: str, computer_id: str, project_id: str,
                     event: dict) -> None: ...


class NullPushSender:
    async def notify(self, **kwargs) -> None:
        return None


@dataclass
class _AckWaiter:
    """One forwarded event, until every live phone acks it or the wait runs out."""

    seq: int
    expected: set[str]
    acked: set[str] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)


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
        self._ack_waiters: dict[tuple[str, str], list[_AckWaiter]] = {}  # (computer, project) -> events in flight
        self._tasks: set[asyncio.Task] = set()

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
                log.info("computer %s (%s) registered, token %s", computer_id, hello.get("computer_name", ""),
                         _short(token))
            elif row["computer_id"] != computer_id or row["role"] != "computer":
                log.warning("computer hello refused: token %s is bound elsewhere", _short(token))
                await self._error(ws, "token is bound to a different computer")
                return None
            else:
                await self._exec("UPDATE devices SET computer_name=?, last_seen=? WHERE token=?",
                                 (hello.get("computer_name", ""), _now_s(), token))
                log.info("computer %s (%s) connected", computer_id, hello.get("computer_name", ""))
            return Conn(ws=ws, token=token, role="computer", computer_id=computer_id)
        if row is None or row["role"] != "phone":
            log.warning("phone hello refused: unknown token %s", _short(token))
            await self._error(ws, "unknown phone token; pair with the computer's QR code")
            return None
        await self._exec("UPDATE devices SET platform=?, push_token=?, last_seen=? WHERE token=?",
                         (hello.get("platform", ""), hello.get("push_token", ""), _now_s(), token))
        log.info("phone %s (%s, push %s) connected to computer %s", _short(token), hello.get("platform") or "?",
                 "yes" if hello.get("push_token") else "no", row["computer_id"])
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
            "projects": self._on_projects, "request": self._on_request, "response": self._on_response,
            "project_delete": self._on_project_delete, "forget_phone": self._on_forget_phone,
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
        log.info("computer %s registered phone token %s (QR shown)", conn.computer_id, _short(token))

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
        live_tokens = {c.token for c in self._peers(conn.computer_id, role="phone")}
        await self._forward(conn.computer_id, {"type": "event", "project_id": project_id,
                                               "seq": seq, "event": event}, role="phone")
        if not live_tokens:
            await self._push(conn.computer_id, project_id, event)
            return
        task = asyncio.create_task(self._push_unless_acked(conn.computer_id, project_id, seq, event, live_tokens))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _push_unless_acked(self, computer_id: str, project_id: str, seq: int, event: dict,
                                 live_tokens: set[str]) -> None:
        """Wait for the live phones to ack the event; push everyone who did not."""
        waiter = _AckWaiter(seq=seq, expected=live_tokens)
        key = (computer_id, project_id)
        self._ack_waiters.setdefault(key, []).append(waiter)
        try:
            await asyncio.wait_for(waiter.done.wait(), ACK_WAIT_S)
        except TimeoutError:
            pass
        finally:
            waiters = self._ack_waiters.get(key, [])
            if waiter in waiters:
                waiters.remove(waiter)
            if not waiters:
                self._ack_waiters.pop(key, None)
        await self._push(computer_id, project_id, event, skip_tokens=waiter.acked)

    async def _push(self, computer_id: str, project_id: str, event: dict,
                    skip_tokens: set[str] = frozenset()) -> None:
        # A permission question under auto_approve is answered on the computer within a
        # second; waking the phone for it would only ring for nothing. A "choice" question
        # (custom options) always needs a person, so it pushes regardless.
        payload = event.get("payload") or {}
        if event.get("type") == "question" and (payload.get("kind") or "permission") != "choice":
            row = await self._fetchone("SELECT doc FROM projects WHERE computer_id=? AND project_id=?",
                                       (computer_id, project_id))
            if row is not None and json.loads(row["doc"]).get("auto_approve") is True:
                return
        rows = await self._fetchall(
            "SELECT token, push_token, platform FROM devices WHERE computer_id=? AND role='phone' AND push_token<>''",
            (computer_id,))
        pushed: set[str] = set()  # one phone may hold several tokens (one per scan); one push per phone
        for r in rows:
            if r["token"] in skip_tokens:
                continue  # this phone acked the event: it is on screen
            if r["push_token"] in pushed:
                continue
            pushed.add(r["push_token"])
            try:
                await self.push.notify(push_token=r["push_token"], platform=r["platform"],
                                       computer_id=computer_id, project_id=project_id, event=event)
            except StalePushToken as stale:
                await self._exec("UPDATE devices SET push_token='' WHERE push_token=?", (stale.push_token,))
                print("[relay] push token no longer registered; forgotten", file=sys.stderr, flush=True)
            except Exception as e:  # noqa: BLE001 - a push failure must never break routing
                print(f"[relay] push failed: {e}", file=sys.stderr, flush=True)

    async def _merge_doc(self, table: str, key_sql: str, key: tuple, fields: dict, insert_sql: str) -> dict:
        row = await self._fetchone(f"SELECT doc FROM {table} WHERE {key_sql}", key)
        doc = json.loads(row["doc"]) if row else {}
        doc.update(fields)
        await self._exec(insert_sql, (*key, json.dumps(doc)))
        return doc

    async def _on_state(self, conn: Conn, frame: dict) -> None:
        """A project document merge. The computer writes anything; a phone only the fields
        that are its own to set (the daemon reads them back with `project`)."""
        project_id, fields = frame["project_id"], frame.get("fields") or {}
        if conn.role == "phone":
            fields = {k: v for k, v in fields.items() if k in PHONE_STATE_FIELDS}
            if not fields:
                return
        await self._merge_doc("projects", "computer_id=? AND project_id=?", (conn.computer_id, project_id), fields,
                              "INSERT OR REPLACE INTO projects(computer_id, project_id, doc) VALUES(?,?,?)")
        await self._forward(conn.computer_id, {"type": "state", "project_id": project_id, "fields": fields},
                            exclude=conn.id, role="phone")

    async def _on_computer(self, conn: Conn, frame: dict) -> None:
        """From the computer: a merge into its document. From a phone (with `req`): a read."""
        if conn.role == "phone":
            row = await self._fetchone("SELECT doc FROM computers WHERE computer_id=?", (conn.computer_id,))
            await conn.send({"type": "computer", "req": frame.get("req"),
                             "fields": json.loads(row["doc"]) if row else {}})
            return
        fields = frame.get("fields") or {}
        await self._merge_doc("computers", "computer_id=?", (conn.computer_id,), fields,
                              "INSERT OR REPLACE INTO computers(computer_id, doc) VALUES(?,?)")
        await self._forward(conn.computer_id, {"type": "computer", "fields": fields}, role="phone")

    async def _on_forget_phone(self, conn: Conn, frame: dict) -> None:
        """The phone unlinked (or removed this computer): forget its token and push token, so
        no push reaches it again and the token cannot reconnect."""
        if conn.role != "phone":
            return
        async with self.write_lock:
            await self.db.execute("DELETE FROM devices WHERE token=?", (conn.token,))
            await self.db.execute("DELETE FROM cursors WHERE token=?", (conn.token,))
            await self.db.commit()
        log.info("phone %s forgotten by its own request (unlink), computer %s", _short(conn.token), conn.computer_id)
        await conn.send({"type": "forgotten", "req": frame.get("req")})

    async def _on_project_delete(self, conn: Conn, frame: dict) -> None:
        """Forget a session: its document, feed, commands and cursors (the phone's "remove")."""
        project_id = frame["project_id"]
        async with self.write_lock:
            for table in ("projects", "events", "commands", "cursors", "seqs"):
                await self.db.execute(f"DELETE FROM {table} WHERE computer_id=? AND project_id=?",
                                      (conn.computer_id, project_id))
            await self.db.commit()
        await self._forward(conn.computer_id, {"type": "project_delete", "project_id": project_id}, exclude=conn.id)

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
        project_id, seq = frame["project_id"], int(frame["seq"])
        await self._exec(
            "INSERT OR REPLACE INTO cursors(token, computer_id, project_id, last_ack_seq) VALUES(?,?,?,?)",
            (conn.token, conn.computer_id, project_id, seq))
        for waiter in self._ack_waiters.get((conn.computer_id, project_id), []):
            if waiter.seq <= seq:
                waiter.acked.add(conn.token)
                if waiter.expected <= waiter.acked:
                    waiter.done.set()

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

    async def _on_projects(self, conn: Conn, frame: dict) -> None:
        """Every project document of this computer, keyed by project id (the supervisor's
        registry of sessions, live and stopped)."""
        rows = await self._fetchall("SELECT project_id, doc FROM projects WHERE computer_id=?", (conn.computer_id,))
        await conn.send({"type": "projects", "req": frame.get("req"),
                         "projects": {r["project_id"]: json.loads(r["doc"]) for r in rows}})

    async def _on_request(self, conn: Conn, frame: dict) -> None:
        """A phone asks the computer for something only the computer can do. Live-only:
        with no computer socket the relay answers on its behalf, so the phone never
        waits on a request nobody will see."""
        if conn.role != "phone":
            return
        out = {"type": "request", "req": frame.get("req"), "kind": frame.get("kind"),
               "payload": frame.get("payload") or {}}
        if await self._forward(conn.computer_id, out, role="computer") == 0:
            await conn.send({"type": "response", "req": frame.get("req"), "kind": frame.get("kind"),
                             "payload": {"error": "offline"}})

    async def _on_response(self, conn: Conn, frame: dict) -> None:
        if conn.role != "computer":
            return
        await self._forward(conn.computer_id, {"type": "response", "req": frame.get("req"),
                                               "kind": frame.get("kind"), "payload": frame.get("payload") or {}},
                            role="phone")

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
