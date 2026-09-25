# The protocol contract

This is what the phone apps rely on.
The document shapes and the encryption envelope below are shared with the shipped apps and must not change without a version bump they understand.
The relay frames are ours to evolve, but the phone speaks them too, so treat them as an interface.

## 1. Encryption

Every sensitive string is AES-256-GCM, with a 32-byte key exchanged once by QR code and never sent to the relay.

```
key      : base64 of 32 random bytes                    (~/.aaw/session.key, mode 0600)
envelope : {"iv": base64(12 bytes), "ct": base64(ciphertext), "tag": base64(16 bytes)}
           serialized as compact JSON with the keys in that order
```

`aaw_core/encryption.py` is the reference: `encrypt`, `decrypt`, `is_encrypted`, `decrypt_if_encrypted` (fail-soft: a value that cannot be decrypted is returned as is).
A fixed-key vector lives in `tests/test_encryption.py`.

## 2. Events (computer to phone)

One document per feed item:

```
{"id": "<uuid>", "type": "<event type>", "ts": <epoch ms>, "payload": {...}}
```

| type | payload | encrypted fields |
| --- | --- | --- |
| `message` | `role` (`user` \| `assistant`), `content`, `agent` | `content` |
| `question` | `question`, `context`, `options` (list), `options_enc` (encrypted copy of each option), `kind` (`permission` \| `ask_user` \| `multiselect` \| ...), `timeout_at`, `agent`, and kind-specific extras | `question`, `context`, each of `options_enc` |
| `notification` | `message`, `level` (`info` \| `success` \| `warning` \| `error`) | `message` |
| `reminder` | `reset_at` (epoch s), `reset_label`, agent details | none |

`SENSITIVE_FIELDS` in `aaw_core/transport/base.py` is the authority.
Notifications at `success`, `warning`, and `error` are the ones that push; they are rate limited to 20 per minute per session.
The plaintext copy of every event is appended to `<state dir>/sessions/<session id>.jsonl` on the computer; that file is what `aaw feed` renders and it never leaves the computer.

## 3. Commands (phone to computer, and the host's own queue)

```
{"id": "<hex>", "type": "command" | "answer", "ts": <epoch ms>, "consumed": false,
 "payload": {...}, "deliver_at"?: <epoch ms>, "status"?: "scheduled" | "canceled" | "done" | "failed"}
```

- A prompt: `payload = {"command": "text", "args": "<plaintext or empty>", "args_enc": <encrypted>, "source": "phone" | "cli"}`.
  The daemon reads `args_enc` when present, else `args`.
  Slash commands (`/restart`, `/stop`, `/usage`, `/approve` ...) travel the same way.
- An answer to a question: `type: "answer"`, `payload = {"question_id": "<event id>", "answer_enc": <encrypted>}` (`answer` in plaintext is accepted for old clients).
- A scheduled prompt carries `deliver_at`; the daemon leaves it queued until then and reports `scheduled_count` and `next_scheduled_at` on the project document.
  `status: "canceled"` withdraws it.
- Consuming a command sets `consumed: true` and `status: done | failed` through a `command_update`; the relay stops replaying it.

## 4. The project document (one per session)

Merged field by field (`state` frames); the phone renders the session card from it.

| field | meaning |
| --- | --- |
| `project_id` | the session id (folder basename, or `<basename>-<agent>` for a second agent on one folder) |
| `status` | `running` \| `waiting` (a question is pending) \| `idle` (the agent finished its turn) \| `stopped` |
| `agent` | `claude` \| `codex` \| `gemini` \| `grok` \| `cursor` \| `scoot` |
| `model` | scoot's provider/model, when known |
| `project_path` | the folder, encrypted |
| `last_event_ts`, `last_event_summary` | the card's subtitle; the summary is encrypted |
| `pending_question_id` | the event id of the open question, or `""` |
| `auto_approve` | phone-set: yes/no permission prompts are answered "yes" by the daemon |
| `pending_message` | a prompt queued while a dialog was open (encrypted by the writer) |
| `scheduled_count`, `next_scheduled_at` | the scheduled-prompt badge |

## 5. The computer document

| field | meaning |
| --- | --- |
| `computer_id`, `name` | identity, as in the QR |
| `status` | `online` (supervisor) \| `running` (a daemon) \| `offline` |
| `last_seen` | epoch s |
| `platform` | `linux` \| `macos` |
| `detected_agents` | which agent CLIs are installed |
| `scoot_models` | model ids scoot can serve |
| `daemon_version` | the aaw-core version |
| `session_pct`, `weekly_pct`, `extra_pct` | Claude usage after `/usage` |

## 6. Pairing: the QR payload

```
{"v": 1, "computer_id": "<hex>", "name": "<computer name>", "relay_url": "wss://.../v1/ws",
 "key": "<base64 session key>", "token": "<phone routing token>"}
```

Compact JSON, keys sorted.
The token is registered with the relay before the QR is shown; the key never reaches the relay.
Each `aaw link` mints a new token; earlier ones keep working.

## 7. Relay frames

Documented in the module docstring of `aaw_core/relay/server.py` and summarized in `relay/README.md`.
The phone connects with `hello {role: "phone", token, platform?, push_token?}`, `subscribe`s to a session, `ack`s sequence numbers, sends `command` frames, and reads `history`, `commands`, `project`, `projects`.

The phone's `request` frames the supervisor answers (`response {req, kind, payload}`):

| kind | payload | response payload |
| --- | --- | --- |
| `start_session` | `project_id` | `result`: `started` \| `not_found` \| `error`; `session_id` |
| `stop_session` | `project_id` | `result`: `stopped` \| `not_found` |
| `new_session` | `path_enc`, `agent`, `model?`, `intent?` (`parallel`) | `result`: `started` \| `conflict` (+ `conflict_agents`, `conflict_session_id`) \| `outside_roots` \| `not_found` \| `not_a_dir` \| `agent_unavailable` \| `error`; `session_id` |
| `fs_browse` | `path_enc?` (absent = the first root) | `resolved_path_enc`, `parent_enc`, `at_root`, `entries[{name_enc, kind, is_repo, size?}]`, `truncated`, `error` |
| `fs_fetch` | `path_enc` | `mime`, `size`, `total_chunks`, `chunks[<encrypted base64>]`, `error` |

When no computer socket is live the relay itself answers `{"error": "offline"}`.
