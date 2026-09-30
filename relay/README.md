# The relay

A small Python WebSocket server that connects a headless host to a phone. It is the only hosted piece for free users (we run one; self-hosters run the same with `docker compose`), and it never sees plaintext: every payload is AES-256-GCM ciphertext produced on the computer or the phone, and the relay reads only routing metadata.

**Status:** implemented in `aaw_core/relay/` (Starlette + `websockets` + `aiosqlite`) and covered by tests, including a real host-to-relay-to-phone integration run. Run it with `python -m aaw_core.relay --host 0.0.0.0 --port 8765 --db relay.sqlite` (needs `pip install "aaw-core[relay]"`); put it behind a TLS-terminating reverse proxy for `wss://`. The exact frame protocol and the trust model are documented in the module docstring of `aaw_core/relay/server.py`. Pushes: start it with `--fcm-service-account <firebase service account json>` (or `AAW_FCM_SERVICE_ACCOUNT`) and it wakes phones through FCM HTTP v1 (`aaw_core/relay/push_fcm.py`), sending the same message shape the hosted product does; without one it sends no pushes and the phone works while its app is open. `relay/docker-compose.yml` is the self-host packaging.

## Connection and auth

- Each device opens `wss://<relay>/v1/ws` and sends a `hello` with its `role` (`computer` or `phone`) and `token`.
- A computer token is trusted on first use and bound to its `computer_id` from then on; a phone token must have been registered by that computer (`register_phone`), which is how it rides in the QR code.
  Tokens are routing credentials only; they are **not** the encryption key and cannot decrypt anything.
- A computer keeps outbound sockets open continuously (the supervisor, one daemon per session, a short-lived one per hook run), which is why no ports, tunnels, or VPNs are needed; a phone connects while the app is in the foreground.

## Frames

JSON text messages with a `type`; the full list, with directions and payloads, is the module docstring of `aaw_core/relay/server.py`.
In short:

- computer to phone, stored and forwarded: `event` (feed items, with a per-project `seq` the phone `ack`s), `state` (project document merges), `computer` (computer document merges), `clear_events`.
- either direction, stored until consumed: `command` (a prompt, an answer, a scheduled prompt with `deliver_at`), `command_update`, `command_delete`.
- reads: `history`, `commands`, `project`, `projects`, `computer`.
- either direction: `project_delete` forgets a session; a phone's `state` may set `auto_approve` and `pending_message` only.
- phone to computer, live only: `request` (start a stopped session, start a new one at a browsed folder, list a folder, read a file) answered by the computer's `response`; the relay answers `{error: "offline"}` itself when no computer socket is live.

Event and command payload shapes, and which fields are encrypted, are the contract with the phone apps: see `docs/protocol.contract.md`. They must not change.

## Store-and-forward and push

- On a computer `event`: assign a per-`(computer_id, project_id)` monotonic `seq`, persist it (90 days), forward it to every live phone socket, then wait up to 4 s for each phone's `ack`. Every registered phone that did not ack (no socket, or a socket left behind by a killed app) gets a wake-up push (FCM/APNs, content-free) through the `PushSender` the deployment plugs in.
- On phone `subscribe`: replay every event with `seq` greater than the phone's last `ack`, then go live.
- A `command` is buffered until the computer marks it consumed (30 days at most), and every unconsumed command is replayed to the computer on each connect.

## Retention, logging, backup

- Events are kept 90 days and commands 30 days (`EVENT_TTL_DAYS`, `COMMAND_TTL_DAYS` in `aaw_core/transport/base.py`); expired rows are deleted when a computer connects. Device tokens stay until the phone unlinks (`forget_phone`) or the computer is re-paired.
- The relay logs one line per pairing event (computer registered or connected, phone token registered, phone connected, phone forgotten, refused hellos) with the first characters of the token only, so the store's state can be followed from the process log without opening the database.
- `python -m aaw_core.relay.backup --db relay.sqlite --dir backups --keep 14` takes a consistent copy while the relay runs (SQLite online backup, the store is in WAL mode) and prunes to the newest 14. Run it from cron or a systemd timer; copy the directory off the box. The store holds encrypted events, commands and device tokens: losing it costs replay and re-pairing, never a key.

## Store schema (SQLite)

```
devices(token PK, computer_id, role, computer_name, platform, push_token, last_seen)
events(id PK, computer_id, project_id, seq, ts, type, payload, expires_at)
seqs(computer_id, project_id, last_seq)
cursors(token, computer_id, project_id, last_ack_seq)
commands(id PK, computer_id, project_id, ts, doc, consumed, expires_at)
projects(computer_id, project_id, doc)
computers(computer_id PK, doc)
```

`payload` and `doc` hold the documents as the devices wrote them: every sensitive field inside is ciphertext.

## Capacity

One tuned node holds tens of thousands of idle sockets on a small box; message rates in this application are tiny. Scale by sharding on the computer owner; users are independent, so there is no cross-node state.

## Store versions and rollback

The store carries a version (`meta.store_version`).
A relay older than the store refuses to start with a clear message rather than silently rejecting every token.
When a relay upgrades the store (0.1.3 hashes the device and cursor tokens at rest), it first writes a consistent snapshot next to it, `relay.sqlite.pre-v2`.
To roll back: stop the relay, replace `relay.sqlite` with the snapshot (and delete the `-wal`/`-shm` files), and start the previous version; only the events since the upgrade are lost.
