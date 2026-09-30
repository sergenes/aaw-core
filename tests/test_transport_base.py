"""Transport base: encrypted fields, document shapes, the delivery gate, the local log."""

from __future__ import annotations

import json

from aaw_core.encryption import decrypt, generate_key_b64, is_encrypted
from aaw_core.transport.base import (
    SENSITIVE_FIELDS,
    LocalLog,
    RateLimiter,
    command_text,
    decrypt_event_entry,
    encrypt_payload,
    make_command,
    make_event,
    select_deliverable,
)

KEY = generate_key_b64()


def test_sensitive_fields_are_the_contract():
    # Changing these would break the shipped phone apps.
    assert SENSITIVE_FIELDS == {
        "message": ("content",),
        "question": ("question", "context"),
        "notification": ("message",),
    }


def test_encrypt_payload_only_touches_sensitive_fields():
    out = encrypt_payload("message", {"role": "assistant", "content": "hi", "agent": "claude"}, KEY)
    assert out["role"] == "assistant" and out["agent"] == "claude"
    assert is_encrypted(out["content"]) and decrypt(out["content"], KEY) == "hi"
    assert encrypt_payload("message", {"content": "x"}, None) == {"content": "x"}  # no key: untouched
    assert encrypt_payload("status", {"content": "x"}, KEY) == {"content": "x"}  # unknown type: untouched


def test_question_options_get_an_encrypted_copy_alongside_plaintext():
    out = encrypt_payload("question", {"question": "Allow?", "context": "", "options": ["Yes", "No"]}, KEY)
    assert out["options"] == ["Yes", "No"]
    assert [decrypt(o, KEY) for o in out["options_enc"]] == ["Yes", "No"]
    assert is_encrypted(out["question"])
    assert out["context"] == ""  # empty fields are not encrypted


def test_make_event_returns_stored_doc_and_plaintext_entry():
    stored, entry = make_event("message", {"role": "user", "content": "secret"}, KEY)
    assert stored["id"] == entry["id"] and stored["ts"] == entry["ts"]
    assert is_encrypted(stored["payload"]["content"])
    assert entry["content"] == "secret" and entry["type"] == "message"
    assert decrypt_event_entry({"type": "message", "content": stored["payload"]["content"]}, KEY)["content"] == "secret"


def test_make_command_and_command_text():
    doc = make_command("run tests", KEY, deliver_at=1_800_000_000_000)
    assert doc["type"] == "command" and doc["consumed"] is False
    assert doc["status"] == "scheduled" and doc["deliver_at"] == 1_800_000_000_000
    assert doc["payload"]["args"] == "run tests" and is_encrypted(doc["payload"]["args_enc"])
    assert command_text(doc, KEY) == "run tests"
    assert "deliver_at" not in make_command("now", KEY)
    assert "args_enc" not in make_command("plain", None)["payload"]


def test_select_deliverable_gate():
    now = 1_000_000
    due = make_command("due", KEY)
    due["ts"] = 5
    older = make_command("older", KEY)
    older["ts"] = 1
    future = make_command("later", KEY, deliver_at=now + 60_000)
    sooner = make_command("sooner", KEY, deliver_at=now + 10_000)
    canceled = make_command("cancel me", KEY, deliver_at=now + 5_000)
    canceled["status"] = "canceled"
    answer = {"id": "a1", "type": "answer", "ts": 2, "consumed": False, "payload": {"question_id": "q"}}
    consumed = make_command("done", KEY)
    consumed["consumed"] = True

    deliverable, cancels, scheduled, next_at = select_deliverable(
        [due, older, future, sooner, canceled, answer, consumed], KEY, at_ms=now)

    assert [d["payload"]["args"] for d in deliverable] == ["older", "due"]  # oldest first
    assert cancels == [canceled]
    assert scheduled == 2 and next_at == now + 10_000
    # the encrypted copy wins over the plaintext one
    tampered = make_command("plain", KEY)
    tampered["payload"]["args"] = "stale plaintext"
    (d,), _, _, _ = select_deliverable([tampered], KEY, at_ms=now)
    assert d["payload"]["args"] == "plain"


def test_local_log_lifecycle(tmp_path):
    log = LocalLog(tmp_path / "sessions", "proj")
    assert not log.exists() and log.read() == []
    log.init()
    assert log.exists() and log.read() == []
    log.append({"id": "1", "type": "message", "ts": 1, "content": "a"})
    log.append({"id": "2", "type": "message", "ts": 2, "content": "b"})
    assert [e["content"] for e in log.read()] == ["a", "b"]
    log.init(is_reconnect=True)  # a reconnect keeps the history
    assert len(log.read()) == 2
    log.init()  # a fresh session truncates
    assert log.read() == []
    log.path.write_text("not json\n" + json.dumps({"id": "3", "type": "message", "ts": 3}) + "\n")
    assert [e["id"] for e in log.read()] == ["3"]  # bad lines are skipped
    log.delete()
    assert not log.exists()
    log.delete()  # idempotent


def test_rate_limiter():
    rl = RateLimiter(max_events=2, window_seconds=60)
    assert rl.allow() and rl.allow() and not rl.allow()


# ── a prompt reported by two writers lands once ──────────────────────────────


def _user(content, ts):
    return {"id": f"e{ts}", "type": "message", "ts": ts, "role": "user", "content": content}


def _users(log):
    return [e["content"] for e in log.read() if e.get("role") == "user"]


def test_hook_then_daemon_echo_is_one_entry(tmp_path):
    from aaw_core.transport.base import LocalLog

    log = LocalLog(tmp_path, "p")
    assert log.append_user_message_once(_user("hi", 1000), via="hook")
    assert not log.append_user_message_once(_user("hi", 2500), via="daemon")
    assert _users(log) == ["hi"]


def test_daemon_then_hook_is_one_entry(tmp_path):
    from aaw_core.transport.base import LocalLog

    log = LocalLog(tmp_path, "p")
    assert log.append_user_message_once(_user("hi", 1000), via="daemon")
    assert not log.append_user_message_once(_user("hi", 1200), via="hook")
    assert _users(log) == ["hi"]


def test_the_same_prompt_sent_twice_stays_twice(tmp_path):
    from aaw_core.transport.base import LocalLog

    log = LocalLog(tmp_path, "p")
    # "yes", then "yes" again: each reported by both writers, in either order
    log.append_user_message_once(_user("yes", 1000), via="hook")
    log.append_user_message_once(_user("yes", 1500), via="daemon")
    log.append_user_message_once(_user("yes", 3000), via="daemon")
    log.append_user_message_once(_user("yes", 3200), via="hook")
    assert _users(log) == ["yes", "yes"]


def test_only_one_writer_always_writes(tmp_path):
    from aaw_core.transport.base import LocalLog

    log = LocalLog(tmp_path, "p")  # Gemini, Grok, Cursor: the daemon is the only writer
    assert log.append_user_message_once(_user("go", 1000), via="daemon")
    assert log.append_user_message_once(_user("go", 2000), via="daemon")
    assert _users(log) == ["go", "go"]


def test_outside_the_window_or_other_text_is_written(tmp_path):
    from aaw_core.transport.base import USER_ECHO_WINDOW_MS, LocalLog

    log = LocalLog(tmp_path, "p")
    log.append_user_message_once(_user("hi", 1000), via="hook")
    assert log.append_user_message_once(_user("hi", 1000 + USER_ECHO_WINDOW_MS + 1), via="daemon")
    assert log.append_user_message_once(_user("other", 1100), via="daemon")
    assert _users(log) == ["hi", "hi", "other"]


def test_answer_echo_then_identical_prompt_keeps_both(tmp_path):
    from aaw_core.transport.base import LocalLog

    log = LocalLog(tmp_path, "p")
    # The computer echoes a question's answer "Yes", the phone then sends the
    # prompt "Yes", and the agent's hook reports that prompt as well.
    assert log.append_user_message_once(_user("Yes", 1000), via="daemon")
    assert log.append_user_message_once(_user("Yes", 3000), via="daemon")
    assert not log.append_user_message_once(_user("Yes", 3300), via="hook")
    assert _users(log) == ["Yes", "Yes"]
