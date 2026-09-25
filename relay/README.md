# The relay

A small Python WebSocket server that connects a headless host to a phone. It is the only hosted piece for free users (we run one; self-hosters run the same with `docker compose`), and it never sees plaintext: every payload is AES-256-GCM ciphertext produced on the computer or the phone, and the relay reads only routing metadata.

**Status:** implemented in `aaw_core/relay/` (Starlette + `websockets` + `aiosqlite`) and covered by tests, including a real host-to-relay-to-phone integration run. Run it with `python -m aaw_core.relay --host 0.0.0.0 --port 8765 --db relay.sqlite` (needs `pip install "aaw-core[relay]"`); put it behind a TLS-terminating reverse proxy for `wss://`. The exact frame protocol and the trust model are documented in the module docstring of `aaw_core/relay/server.py`. Not yet built: the `docker compose` packaging and a real FCM/APNs `PushSender` (the hosted deployment plugs one in; the default is a no-op).

## Connection and auth

- Each device opens `wss://<relay>/v1/ws` and sends `{"type": "hello", "token": "<pairing token>"}`.
- The pairing token is minted at QR time and maps to `(computer_id, role, push_token, platform)`. It is a routing credential only; it is **not** the encryption key and cannot decrypt anything.
- `role` is `computer` or `phone`. A computer keeps one outbound socket open continuously (which is why no ports, tunnels, or VPNs are needed); a phone connects while the app is in the foreground.

## Envelope

```json
{ "type": "event | command | ack | heartbeat | presence",
  "id": "<uuid>", "computer_id": "...", "project_id": "...",
  "seq": 1234, "ts": 1699999999000, "payload_enc": "<AES-GCM blob>" }
```

Event and command payload shapes, and which fields are encrypted, are the contract with the phone apps: see `docs/protocol.contract.md`. They must not change.

## Store-and-forward and push

- On a computer `event`: assign a per-`(computer_id, project_id)` monotonic `seq`, persist it briefly, and forward immediately if the phone has a live socket; otherwise trigger a wake-up push (FCM/APNs, content-free) to the phone's `push_token`.
- On phone connect: replay every event with `seq` greater than the phone's last `ack`, then go live.
- A `command` (prompt, answer, scheduled prompt with `deliver_at`) is buffered until the computer acks it. Both directions carry a TTL, so nothing lingers.

## Buffer schema (SQLite)

```
devices(token PK, computer_id, role, push_token, platform, last_seen)
events(id PK, computer_id, project_id, seq, ts, payload_enc, expires_at)
cursors(device, computer_id, project_id, last_ack_seq)
commands(id PK, computer_id, project_id, ts, payload_enc, consumed, expires_at)
```

## Capacity

One tuned node holds tens of thousands of idle sockets on a small box; message rates in this application are tiny. Scale by sharding on the computer owner; users are independent, so there is no cross-node state.
