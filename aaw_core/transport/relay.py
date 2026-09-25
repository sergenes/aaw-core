"""The relay transport: one outbound WebSocket from the computer to a relay.

The daemon is single-threaded and synchronous, so this transport runs the socket on
a background thread with its own asyncio loop and exposes the same synchronous
method names as the original Firestore transport (``write_event``,
``poll_commands``, ``mark_command_done``, ``set_project_status``, ...), so the
daemon and hooks port with minimal change.

Outbound documents are queued and survive a disconnect (they flush after the
reconnect). Inbound commands are kept in an in-memory inbox; the relay re-sends
every unconsumed command on each connect, so nothing is lost across restarts.
Everything sensitive is encrypted before it leaves this process (see
``transport.base``); the relay only ever sees ciphertext and routing metadata.
"""

from __future__ import annotations

import asyncio
import json
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

import websockets

from aaw_core.transport.base import (
    PUSHABLE_NOTIFICATION_LEVELS,
    LocalLog,
    RateLimiter,
    command_text,
    decrypt_event_entry,
    decrypt_field,
    encrypt_field,
    make_command,
    make_event,
    now_ms,
    select_deliverable,
)

_REQUEST_TIMEOUT = 15.0
_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 30.0


def new_token() -> str:
    """An opaque routing token (not an encryption key)."""
    return secrets.token_urlsafe(32)


class RelayTransport:
    def __init__(self, *, relay_url: str, token: str, computer_id: str, project_id: str,
                 sessions_dir: Path, computer_name: str = "", enc_key: str | None = None):
        self.relay_url = relay_url
        self.token = token
        self.computer_id = computer_id
        self.computer_name = computer_name
        self.project_id = project_id
        self._enc_key = enc_key
        self._log = LocalLog(sessions_dir, project_id)
        self._question_id: str | None = None
        self._notif_limiter = RateLimiter(max_events=20, window_seconds=60)
        self._sched_last: tuple | None = None

        self._lock = threading.Lock()
        self._inbox: dict[str, dict] = {}  # unconsumed commands for this project, by id
        self._pending: dict[str, Future] = {}  # request id -> reply future
        self.requests: queue.Queue[dict] = queue.Queue()  # phone requests for the supervisor to answer
        self._buffer: list[dict] = []  # outbound frames queued before/without a loop
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ws = None  # the live socket, for stop() to close from another thread
        self.connected = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> RelayTransport:
        if self._thread is None:
            self._thread = threading.Thread(target=self._thread_main, name="aaw-relay", daemon=True)
            self._thread.start()
        return self

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until every queued outbound frame has been sent. Best effort: False on timeout
        or when there is no live loop. Hooks call this before exiting so a single event is not
        lost to process exit."""
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(queue.join(), loop).result(timeout)
            return True
        except (TimeoutError, RuntimeError):
            return False

    def stop(self, flush_timeout: float = 10.0) -> None:
        """Flush what is queued, then close the socket and stop the background thread."""
        if self.connected.is_set():
            self.flush(flush_timeout)
        self._stop.set()
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop).result(5)
            except (TimeoutError, RuntimeError, OSError, websockets.WebSocketException):
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def wait_connected(self, timeout: float = 10.0) -> bool:
        return self.connected.wait(timeout)

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        with self._lock:
            for frame in self._buffer:
                self._queue.put_nowait(frame)
            self._buffer.clear()
        backoff = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.relay_url, max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "type": "hello", "role": "computer", "token": self.token,
                        "computer_id": self.computer_id, "computer_name": self.computer_name,
                    }))
                    welcome = json.loads(await asyncio.wait_for(ws.recv(), _REQUEST_TIMEOUT))
                    if welcome.get("type") != "welcome":
                        raise ConnectionError(welcome.get("message", "relay refused the hello"))
                    self.connected.set()
                    backoff = _RECONNECT_MIN
                    await self._session(ws)
            except (TimeoutError, OSError, websockets.WebSocketException, ConnectionError) as e:
                print(f"[relay] disconnected: {e}", file=sys.stderr, flush=True)
            finally:
                self.connected.clear()
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)

    async def _session(self, ws) -> None:
        async def sender():
            while True:
                frame = await self._queue.get()
                try:
                    await ws.send(json.dumps(frame))
                finally:
                    self._queue.task_done()  # lets flush() know the queue drained

        async def receiver():
            async for raw in ws:
                self._on_frame(json.loads(raw))

        self._ws = ws
        send_task = asyncio.create_task(sender())
        try:
            await receiver()
        finally:
            self._ws = None
            send_task.cancel()

    # ── frames ────────────────────────────────────────────────────────────────

    def _send(self, frame: dict) -> None:
        loop, queue = self._loop, self._queue
        if loop is not None and queue is not None:
            loop.call_soon_threadsafe(queue.put_nowait, frame)
        else:
            with self._lock:
                self._buffer.append(frame)

    def _request(self, frame: dict, timeout: float = _REQUEST_TIMEOUT) -> dict:
        req = uuid.uuid4().hex
        fut: Future = Future()
        with self._lock:
            self._pending[req] = fut
        self._send(dict(frame, req=req))
        try:
            return fut.result(timeout)
        finally:
            with self._lock:
                self._pending.pop(req, None)

    def _on_frame(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "command" and frame.get("project_id") == self.project_id:
            doc = frame.get("command") or {}
            if doc.get("id"):
                with self._lock:
                    if doc.get("consumed"):
                        self._inbox.pop(doc["id"], None)
                    else:
                        self._inbox[doc["id"]] = doc
        elif kind == "command_update" and frame.get("project_id") == self.project_id:
            with self._lock:
                doc = self._inbox.get(frame.get("command_id", ""))
                if doc is not None:
                    doc.update(frame.get("fields") or {})
                    if doc.get("consumed"):
                        self._inbox.pop(doc["id"], None)
        elif kind == "command_delete" and frame.get("project_id") == self.project_id:
            with self._lock:
                self._inbox.pop(frame.get("command_id", ""), None)
        elif kind in ("history", "commands", "project", "projects") and frame.get("req"):
            with self._lock:
                fut = self._pending.get(frame["req"])
            if fut is not None and not fut.done():
                fut.set_result(frame)
        elif kind == "request" and frame.get("req"):
            self.requests.put(frame)
        elif kind == "error":
            print(f"[relay] error: {frame.get('message')}", file=sys.stderr, flush=True)

    # ── pairing ───────────────────────────────────────────────────────────────

    def register_phone_token(self) -> str:
        """Mint a phone routing token, tell the relay about it, and return it for the QR."""
        token = new_token()
        self._send({"type": "register_phone", "phone_token": token})
        return token

    # ── field encryption + local log (same names as before) ───────────────────

    def encrypt_field(self, value: str) -> str:
        return encrypt_field(value, self._enc_key)

    def decrypt_field(self, value: str) -> str:
        return decrypt_field(value, self._enc_key)

    def has_local_log(self) -> bool:
        return self._log.exists()

    def init_local_log(self, is_reconnect: bool = False) -> None:
        self._log.init(is_reconnect)

    def delete_local_log(self) -> None:
        self._log.delete()

    # ── computer + project docs ───────────────────────────────────────────────

    def update_computer(self, status: str | None = None, **extra) -> None:
        fields = {"last_seen": int(time.time()), "computer_id": self.computer_id, "name": self.computer_name}
        if status:
            fields["status"] = status
        fields.update(extra)
        self._send({"type": "computer", "fields": fields})

    def set_project_status(self, status: str, last_event_summary: str = "", pending_question_id=None) -> None:
        fields = {"status": status, "last_event_ts": int(time.time()), "project_id": self.project_id}
        if last_event_summary:
            fields["last_event_summary"] = self.encrypt_field(last_event_summary)
        if pending_question_id is not None:
            fields["pending_question_id"] = pending_question_id
        self.update_project(**fields)

    def update_project(self, **fields) -> None:
        self._send({"type": "state", "project_id": self.project_id, "fields": fields})

    def set_project_fields(self, project_id: str, fields: dict) -> None:
        """Merge fields into any project's document (the supervisor's reconcile writes)."""
        self._send({"type": "state", "project_id": project_id, "fields": fields})

    def get_project(self) -> dict:
        """The project document as the relay holds it (status, auto_approve, pending_question_id, ...)."""
        reply = self._request({"type": "project", "project_id": self.project_id})
        return reply.get("fields") or {}

    def clear_events(self) -> None:
        """Wipe this project's retained feed on the relay (the user ran /clear in the agent)."""
        self._send({"type": "clear_events", "project_id": self.project_id})

    def list_projects(self) -> dict[str, dict]:
        """Every project document of this computer, by project id (live and stopped sessions)."""
        reply = self._request({"type": "projects"})
        return reply.get("projects") or {}

    def list_commands(self, project_id: str) -> list:
        """The unconsumed command documents of any project on this computer (raw, undecrypted)."""
        reply = self._request({"type": "commands", "project_id": project_id})
        return reply.get("commands") or []

    # ── phone requests (answered by the supervisor) ───────────────────────────

    def respond(self, request: dict, payload: dict) -> None:
        """Answer one frame taken from ``requests``."""
        self._send({"type": "response", "req": request.get("req"), "kind": request.get("kind"), "payload": payload})

    # ── events ────────────────────────────────────────────────────────────────

    def write_event(self, event_type: str, payload: dict) -> str:
        if (event_type == "notification" and payload.get("level") in PUSHABLE_NOTIFICATION_LEVELS
                and not self._notif_limiter.allow()):
            print(f"[relay] rate limit: notification dropped (level={payload.get('level')})", flush=True)
            return ""
        stored, entry = make_event(event_type, payload, self._enc_key)
        self._log.append(entry)  # local log first: the feed must work even if the relay is down
        self._send({"type": "event", "project_id": self.project_id, "event": stored})
        return stored["id"]

    def read_events(self, limit: int | None = 2000) -> list:
        """The retained history from the relay, oldest first, decrypted, flattened like the local log."""
        reply = self._request({"type": "history", "project_id": self.project_id, "limit": limit or 0})
        out = []
        for doc in reply.get("events", []):
            entry = {"id": doc.get("id"), "type": doc.get("type"), "ts": doc.get("ts", 0)}
            entry.update(doc.get("payload") or {})
            out.append(decrypt_event_entry(entry, self._enc_key))
        return out

    def write_notification(self, message: str, level: str, set_running: bool = False) -> None:
        self.write_event("notification", {"message": message, "level": level})
        if set_running:
            self.set_project_status("running", last_event_summary=message[:80])
        else:
            self.update_project(last_event_summary=self.encrypt_field(message[:80]), last_event_ts=int(time.time()))

    # ── questions and answers ─────────────────────────────────────────────────

    def send_question(self, project: str, agent: str, question: str, context: str = "",
                      options: list | None = None, kind: str = "permission") -> None:
        payload = {
            "question": question, "context": context, "options": options or [],
            "kind": kind, "timeout_at": int(time.time()) + 600, "agent": agent,
        }
        self._question_id = self.write_event("question", payload)
        self.set_project_status("waiting", last_event_summary=question[:80], pending_question_id=self._question_id)

    def send_notification(self, project: str, agent: str, message: str) -> None:
        """A desktop notification on the computer itself (macOS or Linux), best effort."""
        title = f"[{project}/{agent}] {agent.capitalize()} needs input"
        body = message[:120] + "…" if len(message) > 120 else message
        try:
            if sys.platform == "darwin":
                script = (f'display notification "{body.replace(chr(34), chr(92) + chr(34))}" '
                          f'with title "{title.replace(chr(34), chr(92) + chr(34))}" sound name "Glass"')
                subprocess.run(["osascript", "-e", script], timeout=5, check=False, capture_output=True)
            elif shutil.which("notify-send"):
                subprocess.run(["notify-send", title, body], timeout=5, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass

    def _take_answer(self, question_id: str | None) -> str | None:
        if not question_id:
            return None
        with self._lock:
            for doc in list(self._inbox.values()):
                p = doc.get("payload") or {}
                if doc.get("type") == "answer" and p.get("question_id") == question_id:
                    self._inbox.pop(doc["id"], None)
                    break
            else:
                return None
        self._send({"type": "command_update", "project_id": self.project_id, "command_id": doc["id"],
                    "fields": {"consumed": True}})
        self.set_project_status("running", pending_question_id="")
        answer = self.decrypt_field(p["answer_enc"]) if p.get("answer_enc") else p.get("answer", "")
        # The answer's trace in the feed, right after its question, once the question card
        # is gone. The phone cannot write events on the relay, so the computer does it.
        if answer:
            self.write_event("message", {"role": "user", "content": answer})
        return answer

    def poll_answer(self, deadline: float) -> str | None:
        while time.time() < deadline:
            answer = self._take_answer(self._question_id)
            if answer is not None:
                return answer
            time.sleep(0.5)
        return None

    def poll_perm_answer_once(self, question_id: str) -> str | None:
        return self._take_answer(question_id)

    # ── daemon command polling ────────────────────────────────────────────────

    def poll_commands(self) -> list:
        with self._lock:
            snapshot = [dict(d) for d in self._inbox.values()]
        deliverable, canceled, scheduled, next_at = select_deliverable(snapshot, self._enc_key)
        for doc in canceled:
            self.mark_command_done(doc["id"], ok=True)
        sched = (scheduled, next_at)
        if sched != self._sched_last:
            self.update_project(scheduled_count=scheduled, next_scheduled_at=next_at or 0)
            self._sched_last = sched
        return deliverable

    def mark_command_done(self, command_id: str, ok: bool = True) -> None:
        if not command_id:
            return
        with self._lock:
            self._inbox.pop(command_id, None)
        self._send({"type": "command_update", "project_id": self.project_id, "command_id": command_id,
                    "fields": {"consumed": True, "status": "done" if ok else "failed"}})

    # ── host-side command writes (aaw send / schedule / scheduled) ────────────

    def write_command(self, args: str, *, deliver_at: int | None = None,
                      status: str | None = None, source: str = "cli") -> str:
        doc = make_command(args, self._enc_key, deliver_at=deliver_at, status=status, source=source)
        with self._lock:
            self._inbox[doc["id"]] = doc
        self._send({"type": "command", "project_id": self.project_id, "command": doc})
        return doc["id"]

    def list_scheduled(self) -> list:
        at = now_ms()
        with self._lock:
            docs = [dict(d) for d in self._inbox.values()]
        out = [
            {"id": d["id"], "deliver_at": d["deliver_at"], "text": command_text(d, self._enc_key)}
            for d in docs
            if d.get("type") == "command" and d.get("status") != "canceled" and (d.get("deliver_at") or 0) > at
        ]
        out.sort(key=lambda x: x["deliver_at"])
        return out

    def cancel_command(self, command_id: str) -> None:
        with self._lock:
            self._inbox.pop(command_id, None)
        self._send({"type": "command_delete", "project_id": self.project_id, "command_id": command_id})

    def update_command(self, command_id: str, args: str, deliver_at: int) -> None:
        fresh = make_command(args, self._enc_key, deliver_at=deliver_at)
        fields = {"deliver_at": int(deliver_at), "ts": fresh["ts"], "payload": fresh["payload"]}
        with self._lock:
            doc = self._inbox.get(command_id)
            if doc is not None:
                doc.update(fields)
        self._send({"type": "command_update", "project_id": self.project_id, "command_id": command_id,
                    "fields": fields})
