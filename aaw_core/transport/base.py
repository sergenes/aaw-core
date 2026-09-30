"""Backend-agnostic transport pieces shared by every transport implementation.

What lives here is the contract with the phone apps and the daemon, independent of
how bytes reach the phone: which fields are encrypted, the event and command
document shapes, the local per-session log the ``aaw feed`` command reads, and the
scheduled-prompt delivery gate. A concrete transport (``relay.RelayTransport``)
only has to move documents; everything about their shape is decided here.

Compatibility contract: the shapes and encrypted fields below are what the
already-shipped phone apps read and write. Do not change them.
"""

from __future__ import annotations

import fcntl
import json
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from aaw_core.encryption import decrypt_if_encrypted, encrypt

# Fields encrypted per event type. Everything else stays plaintext for routing and UI.
SENSITIVE_FIELDS: dict[str, tuple[str, ...]] = {
    "message": ("content",),
    "question": ("question", "context"),
    "notification": ("message",),
}

# Notification levels that trigger a push on the phone, and therefore get rate limited.
PUSHABLE_NOTIFICATION_LEVELS = {"success", "error", "warning"}

EVENT_TTL_DAYS = 90
COMMAND_TTL_DAYS = 30

# The wire protocol version, sent in hello and echoed back in welcome. Additive changes
# never bump it; a breaking change raises the relay's minimum so an outdated client is
# refused with an actionable message instead of failing strangely. Absent means 1.
PROTO_VERSION = 1


def now_ms() -> int:
    return int(time.time() * 1000)


class RateLimiter:
    """Sliding-window rate limiter for single-threaded use."""

    def __init__(self, max_events: int, window_seconds: int):
        self._max = max_events
        self._window = window_seconds
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._timestamps and self._timestamps[0] < now - self._window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


# ── Field encryption ─────────────────────────────────────────────────────────


def encrypt_field(value: str, key_b64: str | None) -> str:
    """Encrypt one string field if a key is set, else return it unchanged."""
    if not key_b64 or not value:
        return value
    return encrypt(value, key_b64)


def decrypt_field(value: str, key_b64: str | None) -> str:
    """Fail-soft decrypt of one field (plaintext, no key, or a bad envelope come back as-is)."""
    if not key_b64 or not value:
        return value
    return decrypt_if_encrypted(value, key_b64)


def encrypt_payload(event_type: str, payload: dict, key_b64: str | None) -> dict:
    """Return a copy of ``payload`` with the sensitive fields for ``event_type`` encrypted.

    Question options additionally get an encrypted copy in ``options_enc`` while the
    plaintext list stays for older app versions.
    """
    if not key_b64 or event_type not in SENSITIVE_FIELDS:
        return payload
    result = dict(payload)
    for field in SENSITIVE_FIELDS[event_type]:
        if result.get(field):
            result[field] = encrypt(result[field], key_b64)
    if event_type == "question" and isinstance(result.get("options"), list) and result["options"]:
        result["options_enc"] = [encrypt(o, key_b64) for o in result["options"]]
    return result


def decrypt_event_entry(entry: dict, key_b64: str | None) -> dict:
    """Decrypt the sensitive fields of a flattened event entry ({id, type, ts, **payload})."""
    out = dict(entry)
    for field in SENSITIVE_FIELDS.get(out.get("type", ""), ()):
        if out.get(field):
            out[field] = decrypt_field(out[field], key_b64)
    return out


# ── Document shapes ──────────────────────────────────────────────────────────


def make_event(event_type: str, payload: dict, key_b64: str | None) -> tuple[dict, dict]:
    """Build an event document. Returns (stored_doc, plaintext_entry).

    ``stored_doc`` has the encrypted payload and is what travels to the phone;
    ``plaintext_entry`` is the flattened local-log line.
    """
    event_id = str(uuid.uuid4())
    ts = now_ms()  # milliseconds keep user/assistant ordering within one second
    stored = {"id": event_id, "type": event_type, "ts": ts, "payload": encrypt_payload(event_type, payload, key_b64)}
    entry = {"id": event_id, "type": event_type, "ts": ts}
    entry.update(payload)
    return stored, entry


def make_command(args: str, key_b64: str | None, *, deliver_at: int | None = None,
                 status: str | None = None, source: str = "cli") -> dict:
    """Build a text command document as the phone would, with encrypted args alongside plaintext."""
    payload = {"command": "text", "args": args, "source": source}
    enc = encrypt_field(args, key_b64)
    if enc and enc != args:
        payload["args_enc"] = enc
    doc = {"id": uuid.uuid4().hex, "type": "command", "ts": now_ms(), "consumed": False, "payload": payload}
    if deliver_at is not None:
        doc["deliver_at"] = int(deliver_at)
        doc["status"] = status or "scheduled"
    return doc


def command_text(doc: dict, key_b64: str | None) -> str:
    """The prompt text of a command: the encrypted copy when present, else the plaintext one."""
    payload = doc.get("payload") or {}
    if payload.get("args_enc"):
        return decrypt_field(payload["args_enc"], key_b64)
    return payload.get("args", "")


def select_deliverable(commands: list[dict], key_b64: str | None, at_ms: int | None = None) -> tuple[list, list, int, int | None]:
    """The scheduled-prompt delivery gate.

    Given unconsumed command documents, return
    ``(deliverable, canceled, scheduled_count, next_scheduled_at)``:

    - ``deliverable``: commands due now, oldest first, with ``payload.args`` decrypted;
      answers are excluded (they are consumed by the question flow, not the daemon).
    - ``canceled``: commands with ``status == "canceled"`` that should be consumed without running.
    - a future-dated ``deliver_at`` leaves the command queued until its time, on this
      computer's clock, so an agent can be prompted overnight when its quota resets.
    """
    at_ms = now_ms() if at_ms is None else at_ms
    deliverable: list[dict] = []
    canceled: list[dict] = []
    scheduled = 0
    next_at: int | None = None
    for doc in commands:
        if doc.get("type") == "answer" or doc.get("consumed"):
            continue
        if doc.get("status") == "canceled":
            canceled.append(doc)
            continue
        deliver_at = doc.get("deliver_at")
        if deliver_at and deliver_at > at_ms:
            scheduled += 1
            next_at = deliver_at if next_at is None else min(next_at, deliver_at)
            continue
        out = dict(doc)
        payload = dict(doc.get("payload") or {})
        if payload.get("args_enc"):
            payload["args"] = decrypt_field(payload["args_enc"], key_b64)
        out["payload"] = payload
        deliverable.append(out)
    deliverable.sort(key=lambda d: d.get("ts", 0))
    return deliverable, canceled, scheduled, next_at


# ── Local per-session log ────────────────────────────────────────────────────


# How far apart the daemon's echo and the agent's prompt hook may report the same
# prompt: seconds in practice (the daemon waits for Enter to land, up to a few retries).
USER_ECHO_WINDOW_MS = 15_000


class LocalLog:
    """The plaintext per-session JSONL log that ``aaw feed`` renders.

    Truncated at session start, appended on every event; survives a reconnect.
    """

    def __init__(self, sessions_dir: Path, project_id: str):
        self.path = sessions_dir / f"{project_id}.jsonl"

    def exists(self) -> bool:
        return self.path.exists()

    def init(self, is_reconnect: bool = False) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not is_reconnect:
                self.path.write_text("")
        except OSError as e:
            print(f"[transport] local log init failed: {e}", file=sys.stderr, flush=True)

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def append(self, entry: dict) -> None:
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[transport] local log write failed: {e}", file=sys.stderr, flush=True)

    def append_user_message_once(self, entry: dict, via: str, window_ms: int = USER_ECHO_WINDOW_MS) -> bool:
        """Append a user message unless another writer already recorded this same prompt.

        Two writers report a prompt the phone sent: the daemon, once it has typed it, and
        the agent's own prompt hook (Claude, Scoot, Codex), a second or two apart. Each
        tags its line with ``via``. Within the window, a write is skipped when the other
        writers already logged more copies of the same text than this writer has, so
        every sent prompt ends up in the feed exactly once, repeats included ("yes"
        twice is two entries). The check and the append run under a file lock, since the
        hook is a separate process. Returns True when the entry was appended."""
        entry = {**entry, "via": via}
        content = entry.get("content", "")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 64 * 1024))
                    mine = others = 0
                    since = int(entry.get("ts", 0)) - window_ms
                    for line in f.read().splitlines()[-200:]:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if (e.get("type") == "message" and e.get("role") == "user"
                                and e.get("content") == content and int(e.get("ts", 0)) >= since):
                            if e.get("via") == via:
                                mine += 1
                            else:
                                others += 1
                    if others > mine:
                        return False
                    f.seek(0, 2)
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    return True
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except OSError as e:
            print(f"[transport] local log write failed: {e}", file=sys.stderr, flush=True)
            return True

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
