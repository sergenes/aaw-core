"""The FCM push sender: the message shape the apps expect, the service-account JWT, the
token cache, stale-token pruning, and the relay's auto_approve skip."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aaw_core.relay.push_fcm import ENCRYPTED_BODY_BUDGET, FcmPushSender, build_message
from aaw_core.relay.server import StalePushToken

QUESTION = {"id": "q1", "type": "question", "ts": 1,
            "payload": {"question": "ENC(q)", "kind": "permission", "agent": "codex", "options": ["Yes", "No"]}}
NOTE = {"id": "n1", "type": "notification", "ts": 1, "payload": {"message": "ENC(m)", "level": "success"}}


def test_ios_question_message_matches_the_hosted_shape():
    m = build_message(push_token="t", platform="ios", computer_id="c", project_id="p", event=QUESTION)
    assert m["token"] == "t"
    assert m["notification"] == {"title": "❓ Codex needs your input", "body": "Tap to view and respond"}
    assert m["data"] == {"computer_id": "c", "project_id": "p", "event_id": "q1", "type": "question",
                         "kind": "permission", "agent": "codex", "encrypted_body": "ENC(q)"}
    assert m["apns"] == {"payload": {"aps": {"sound": "default", "mutable-content": 1, "category": "PERMISSION_PROMPT"}}}
    assert "android" not in m


def test_choice_questions_get_no_yes_no_category():
    event = {"id": "q2", "type": "question", "payload": {"question": "ENC", "kind": "choice"}}
    m = build_message(push_token="t", platform="ios", computer_id="c", project_id="p", event=event)
    assert "category" not in m["apns"]["payload"]["aps"]
    assert m["data"]["kind"] == "choice" and m["data"]["agent"] == "claude"


def test_android_is_data_only_with_the_title_in_data():
    m = build_message(push_token="t", platform="android", computer_id="c", project_id="p", event=NOTE)
    assert m == {"token": "t", "android": {"priority": "high"}, "data": {
        "computer_id": "c", "project_id": "p", "event_id": "n1", "type": "notification", "kind": "permission",
        "agent": "claude", "encrypted_body": "ENC(m)", "title": "✅ Response ready"}}


@pytest.mark.parametrize("event", [
    {"id": "m", "type": "message", "payload": {"role": "assistant", "content": "x"}},
    {"id": "i", "type": "notification", "payload": {"message": "x", "level": "info"}},
    {"id": "r", "type": "reminder", "payload": {}},
])
def test_only_questions_and_alert_notifications_push(event):
    assert build_message(push_token="t", platform="ios", computer_id="c", project_id="p", event=event) is None


def test_oversized_ciphertext_is_sent_empty_never_truncated():
    big = {"id": "n", "type": "notification", "payload": {"message": "x" * (ENCRYPTED_BODY_BUDGET + 1), "level": "error"}}
    m = build_message(push_token="t", platform="ios", computer_id="c", project_id="p", event=big)
    assert m["data"]["encrypted_body"] == ""
    assert m["notification"]["title"] == "❌ Agents At Work: error"


# ── the sender ──────────────────────────────────────────────────────────────


@pytest.fixture
def account():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return key, {"project_id": "demo-app", "client_email": "relay@demo-app.iam.gserviceaccount.com",
                 "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token"}


class FakeHttp:
    def __init__(self, send_status=200, send_reply=None):
        self.calls: list[tuple[str, dict, bytes]] = []
        self.send_status = send_status
        self.send_reply = send_reply or {}

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if url.endswith("/token"):
            return 200, {"access_token": "at-1", "expires_in": 3600}
        return self.send_status, self.send_reply


def _decode_segment(seg: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def test_assertion_is_a_valid_rs256_jwt(account):
    key, sa = account
    sender = FcmPushSender(sa, post=FakeHttp())
    header, claims, signature = sender.assertion(now=1_700_000_000).split(".")
    assert _decode_segment(header) == {"alg": "RS256", "typ": "JWT"}
    assert _decode_segment(claims) == {"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
                                       "aud": sa["token_uri"], "iat": 1_700_000_000, "exp": 1_700_003_600}
    sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    key.public_key().verify(sig, f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # raises if bad


def test_notify_fetches_the_token_once_and_sends_the_message(account):
    _, sa = account
    http = FakeHttp()
    sender = FcmPushSender(sa, post=http)

    async def run():
        await sender.notify(push_token="tok", platform="ios", computer_id="c", project_id="p", event=QUESTION)
        await sender.notify(push_token="tok", platform="android", computer_id="c", project_id="p", event=NOTE)
        await sender.notify(push_token="tok", platform="ios", computer_id="c", project_id="p",
                            event={"id": "m", "type": "message", "payload": {}})  # not pushed, no calls
    asyncio.run(run())
    urls = [c[0] for c in http.calls]
    assert urls == ["https://oauth2.googleapis.com/token",
                    "https://fcm.googleapis.com/v1/projects/demo-app/messages:send",
                    "https://fcm.googleapis.com/v1/projects/demo-app/messages:send"]
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in http.calls[0][2].decode()
    assert http.calls[1][1]["Authorization"] == "Bearer at-1"
    sent = json.loads(http.calls[1][2])["message"]
    assert sent["token"] == "tok" and sent["data"]["event_id"] == "q1"


def test_expired_token_is_refreshed(account):
    _, sa = account
    http = FakeHttp()
    sender = FcmPushSender(sa, post=http)

    async def run():
        await sender.access_token()
        sender._expires_at = time.time() + 10  # inside the safety margin
        await sender.access_token()
    asyncio.run(run())
    assert [c[0] for c in http.calls].count("https://oauth2.googleapis.com/token") == 2


def test_unregistered_token_raises_stale(account):
    _, sa = account
    http = FakeHttp(send_status=404, send_reply={"error": {"status": "NOT_FOUND", "details": [
        {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": "UNREGISTERED"}]}})
    sender = FcmPushSender(sa, post=http)
    with pytest.raises(StalePushToken) as e:
        asyncio.run(sender.notify(push_token="gone", platform="ios", computer_id="c", project_id="p", event=QUESTION))
    assert e.value.push_token == "gone"


def test_other_failures_are_logged_not_raised(account, capsys):
    _, sa = account
    http = FakeHttp(send_status=500, send_reply={"error": {"message": "backend"}})
    sender = FcmPushSender(sa, post=http)
    asyncio.run(sender.notify(push_token="tok", platform="ios", computer_id="c", project_id="p", event=QUESTION))
    assert "FCM send failed (500)" in capsys.readouterr().err
