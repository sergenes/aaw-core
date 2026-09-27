"""Push through Firebase Cloud Messaging (HTTP v1), for a hosted relay.

The relay never sees plaintext, and neither does this: the push carries routing ids,
a title, and the event's already-encrypted preview, which the phone decrypts with
the session key (the iOS Notification Service Extension, Android's message handler).
The message shape is the one the apps already handle for hosted computers, so a
relay push and a hosted push look the same on the phone.

Credentials: a Firebase service-account JSON (``firebase_messaging`` scope). Its path
is configuration (``--fcm-service-account`` / ``AAW_FCM_SERVICE_ACCOUNT``); nothing in
this repository points at any project. A self-hosted relay without one sends no
pushes, and the phone still works while its app is open.

No HTTP library: two small POSTs (an OAuth token exchange, a send) go through urllib
in a worker thread, and a test injects its own ``post``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from aaw_core.relay.server import StalePushToken, pushes

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
# FCM caps the data payload near 4 KB; the other fields cost a few hundred bytes. A
# ciphertext is never truncated (that breaks its authentication): over budget it is sent
# empty and the phone opens the app to read the event by id.
ENCRYPTED_BODY_BUDGET = 3000
TOKEN_MARGIN_S = 60

PostFn = Callable[[str, dict, bytes], tuple[int, dict]]  # (url, headers, body) -> (status, json)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _post_urllib(url: str, headers: dict, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except ValueError:
            detail = {}
        return e.code, detail


def build_message(*, push_token: str, platform: str, computer_id: str, project_id: str, event: dict) -> dict | None:
    """The FCM v1 message for one event, or None when this event does not push.

    Questions always push; notifications push at success/error/warning; nothing else.
    iOS gets a notification block (the generic fallback when the extension cannot run)
    plus mutable-content so the extension decrypts the real text, and the Yes/No
    approval category for permission questions. Android gets data only, so its
    handler runs even when the app is killed."""
    if not pushes(event):
        return None
    kind = event.get("type", "")
    payload = event.get("payload") or {}
    encrypted = (payload.get("question") if kind == "question" else payload.get("message")) or ""
    if len(encrypted.encode()) > ENCRYPTED_BODY_BUDGET:
        encrypted = ""
    question_kind = payload.get("kind") or "permission"
    agent = payload.get("agent") or "claude"
    if kind == "question":
        title = f"❓ {agent.capitalize()} needs your input"
    elif payload.get("level") == "success":
        title = "✅ Response ready"
    elif payload.get("level") == "error":
        title = "❌ Agents At Work: error"
    else:
        title = "⚠️ Agents At Work: warning"
    data = {
        "computer_id": computer_id, "project_id": project_id, "event_id": str(event.get("id", "")),
        "type": kind, "kind": question_kind, "agent": agent, "encrypted_body": encrypted,
    }
    if platform == "android":
        data["title"] = title
        return {"token": push_token, "data": data, "android": {"priority": "high"}}
    aps: dict = {"sound": "default", "mutable-content": 1}
    if kind == "question" and question_kind != "choice":
        aps["category"] = "PERMISSION_PROMPT"
    body = "Tap to view and respond" if kind == "question" else "Tap to view"
    # apns-collapse-id: the app posts its own notification for an event it also receives
    # over the socket, under the event id; iOS then replaces this one instead of stacking.
    return {"token": push_token, "notification": {"title": title, "body": body}, "data": data,
            "apns": {"headers": {"apns-collapse-id": data["event_id"]}, "payload": {"aps": aps}}}


class FcmPushSender:
    """``PushSender`` over FCM HTTP v1 with a service account."""

    def __init__(self, service_account: dict | str | Path, *, post: PostFn | None = None):
        if not isinstance(service_account, dict):
            service_account = json.loads(Path(service_account).read_text())
        self.project_id = service_account["project_id"]
        self.client_email = service_account["client_email"]
        self.token_uri = service_account.get("token_uri", "https://oauth2.googleapis.com/token")
        self._key = serialization.load_pem_private_key(service_account["private_key"].encode(), password=None)
        self._post = post or _post_urllib
        self._access_token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def assertion(self, now: float | None = None) -> str:
        """A signed JWT (RS256) the token endpoint exchanges for an access token."""
        iat = int(now if now is not None else time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({
            "iss": self.client_email, "scope": FCM_SCOPE, "aud": self.token_uri, "iat": iat, "exp": iat + 3600,
        }, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode()
        signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64url(signature)}"

    async def access_token(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - TOKEN_MARGIN_S:
                return self._access_token
            body = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.assertion(),
            }).encode()
            status, reply = await asyncio.to_thread(
                self._post, self.token_uri, {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if status != 200 or not reply.get("access_token"):
                raise RuntimeError(f"FCM token exchange failed ({status}): {reply.get('error_description') or reply}")
            self._access_token = reply["access_token"]
            self._expires_at = time.time() + float(reply.get("expires_in", 3600))
            return self._access_token

    async def notify(self, *, push_token: str, platform: str, computer_id: str, project_id: str,
                     event: dict) -> None:
        message = build_message(push_token=push_token, platform=platform, computer_id=computer_id,
                                project_id=project_id, event=event)
        if message is None:
            return
        token = await self.access_token()
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        status, reply = await asyncio.to_thread(self._post, url, headers, json.dumps({"message": message}).encode())
        if status == 200:
            return
        error = reply.get("error") or {}
        codes = {d.get("errorCode") for d in error.get("details", []) if isinstance(d, dict)}
        if status == 404 or "UNREGISTERED" in codes or error.get("status") == "NOT_FOUND":
            raise StalePushToken(push_token)
        if status == 401:
            self._access_token = ""  # a revoked token; the next push fetches a fresh one
        print(f"[relay] FCM send failed ({status}): {error.get('message') or reply}", file=sys.stderr, flush=True)
