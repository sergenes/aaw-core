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
| `message` | `role` (`user` \| `assistant`), `content`, `agent`, and `command_id` on the echo of a prompt from the phone (the command it delivered, since 0.1.9), so the phone matches its own prompt by id. In a shared session, since 0.1.16, a guest's prompt or answer also carries `from` (the guest's share id), `from_label` (the name its owner gave it; absent when the share has none) and `from_platform`; a message without `from` is the owner's. The echo of an answer to a question, since 0.1.17, carries `question_id` (the question event it answers) and `question` (that question's text, cut at 2000 characters; absent when the computer no longer knows it and the phone did not quote it), so a feed shows the answer with its question even when the two are apart or the question event is gone; a user message without `question_id` is a prompt | `content`, `from_label`, `question` |
| `question` | `question`, `context`, `options_enc` (each option encrypted; since 0.1.9 the plaintext `options` list is not sent, every relay-era app reads `options_enc`), `kind` (`permission` \| `choice` \| `ask_user` \| `multiselect` \| ...; a pane menu the user opened, such as Claude Code's `/model`, is a `choice` titled by its own header since 0.1.10, and since 0.1.12 its options are one line each, the menu's padded columns joined with " · " ("Fable 5.1 · For your toughest challenges"), which is also the answer text echoed to the feed), `timeout_at`, `agent`, and kind-specific extras | `question`, `context`, each of `options_enc` |
| `notification` | `message`, `level` (`info` \| `success` \| `warning` \| `error`) | `message` |
| `reminder` | `reset_at` (epoch s), `reset_label`, agent details | none |

`SENSITIVE_FIELDS` in `aaw_core/transport/base.py` is the authority.
Notifications at `success`, `warning`, and `error` are the ones that push; they are rate limited to 20 per minute per session, and the relay itself also caps pushes at 20 per minute per computer.
The plaintext copy of every event is appended to `<state dir>/sessions/<session id>.jsonl` on the computer; that file is what `aaw feed` renders and it never leaves the computer.

## 3. Commands (phone to computer, and the host's own queue)

```
{"id": "<hex>", "type": "command" | "answer", "ts": <epoch ms>, "consumed": false,
 "payload": {...}, "deliver_at"?: <epoch ms>, "status"?: "scheduled" | "canceled" | "done" | "failed"}
```

- A prompt: `payload = {"command": "text", "args": "", "args_enc": <encrypted>, "source": "phone" | "cli"}`.
  The daemon reads `args_enc` when present, else `args`. A writer with a key sends `args_enc` only (since 0.1.9; `args` stays empty), so the relay and its backups never hold the prompt.
  Slash commands (`/restart`, `/stop`, `/usage`, `/approve` ...) travel the same way.
- An answer to a question: `type: "answer"`, `payload = {"question_id": "<event id>", "answer_enc": <encrypted>}` (`answer` in plaintext is accepted for old clients; a writer with a key sends `answer_enc` only).
  Since 0.1.17 a phone may add `question_enc`, the question's text as it showed it, encrypted like the answer: the computer quotes it in the answer's echo when it no longer holds the question itself (the daemon restarted in between). The relay stores it untouched.
- A scheduled prompt carries `deliver_at`; the daemon leaves it queued until then and reports `scheduled_count` and `next_scheduled_at` on the project document.
  `status: "canceled"` withdraws it.
- Consuming a command sets `consumed: true` and `status: done | failed` through a `command_update`; the relay stops replaying it.
- Who sent it, since 0.1.16: the relay stores and forwards a guest's command with `from` (the share id, the `id` of its row in `shares`), `from_label_enc` (the label its owner set with `share`, still encrypted; absent when there is none) and `from_platform`.
  The relay writes these and a phone cannot: whatever a phone puts in them is dropped, and the owner's commands carry none.
  A `command_update` whose `fields` rewrite `payload` moves the sender to whoever wrote it (a guest's fields are stamped, the owner's are set to `""`); a change of `deliver_at` or `status` leaves the sender alone.
  The share id is the first 16 hex characters of the SHA-256 of the guest's token, so a guest app can tell its own messages from another guest's.
- Sent from a phone, since 0.1.18: the relay stores and forwards every command a phone wrote with `via: "phone"`; a command from the computer's own tools (`aaw send`, the Mac's Session Feed) carries none.
  Like the sender fields, only the relay writes it.
  It tells the relay whose turn a prompt starts (see "Pausing a shared session's notifications").
- A guest's prompt is typed into the agent behind a short note of who wrote it, `[from guest Anna] ` (or `[from a guest] ` when the share has no label), so the agent can tell the owner from a guest. The feed shows the text as written, with the sender in `from` and `from_label`. A slash command is typed bare, and an answer to a question is not prefixed (the agent sees only the choice); both are attributed in the feed all the same.

## 4. The project document (one per session)

Merged field by field (`state` frames); the phone renders the session card from it.

| field | meaning |
| --- | --- |
| `project_id` | the session id (folder basename, or `<basename>-<agent>` for a second agent on one folder) |
| `status` | `running` \| `waiting` (a question is pending) \| `idle` (the agent finished its turn) \| `stopped` |
| `background_agents` | Claude only: opaque ids of subagents still running, written by the SubagentStart/SubagentStop hooks and cleared on session start, `/restart` and `/stop`. A phone showing an `idle` session with a non-empty list says "Waiting for N background agents to finish". |
| `agent` | `claude` \| `codex` \| `gemini` \| `grok` \| `cursor` \| `scoot` |
| `model` | the model the session answers with, when known: scoot's provider/model, or Claude's and Codex's model id. Since 0.1.11 it comes from the hook input first (Codex names it on every prompt, so it shows as a turn starts and a `/model` switch shows at the next prompt; Claude names it at session start), else from the transcript after each turn. Absent or `""` means unknown; it is never guessed from the agent's config |
| `project_path` | the folder, encrypted |
| `last_event_ts`, `last_event_summary` | the card's subtitle and its "last touched" order; the summary is encrypted. `last_event_ts` moves with activity only (a message, a question, a tool-call summary, a notice, a session start or stop), never with a plain status change such as a restart or an idle check |
| `pending_question_id` | the event id of the open question, or `""` |
| `auto_approve` | phone-set: yes/no permission prompts are answered "yes" by the daemon |
| `pending_message` | a prompt queued while a dialog was open (encrypted by the writer; the daemon wrote its own in the clear until 0.1.16) |
| `pending_command_id` | since 0.1.18, the id of the command that carried `pending_message`, so its echo names its command like any other prompt's |
| `pending_from`, `pending_from_label_enc`, `pending_from_platform` | since 0.1.16, who queued `pending_message`: the relay stamps them when a guest sets it through `state` and empties them when the owner's phone does; the daemon writes them itself when it queues a command, and clears them once the message is typed |
| `scheduled_count`, `next_scheduled_at` | the scheduled-prompt badge |

## 5. The computer document

| field | meaning |
| --- | --- |
| `computer_id`, `name` | identity, as in the QR. `name` is written by the supervisor's heartbeat only; a session daemon's heartbeat carries `last_seen` and `status`, never `name` (since 0.1.9, so ten daemons resolving the name on their own cannot make it flip) |
| `status` | `online` (supervisor) \| `running` (a daemon) \| `offline` |
| `last_seen` | epoch s |
| `platform` | `linux` \| `macos` |
| `detected_agents` | which agent CLIs are installed |
| `scoot_models` | model ids scoot can serve |
| `scoot_errors_enc` | since 0.1.18, why scoot has no models from a provider: an encrypted JSON object `{provider: message}` (Ollama not running, a rejected API key), `""` when every provider answered. `aaw status --json --agents` gives the same map in the clear as `scoot_errors` (`null` when scoot is not installed) |
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

A guest QR (session sharing, phones 2.13 with a 0.1.13 relay) is the same payload with one more field, `"scope": "<project_id>"`, shown by the owner's phone, which holds the key and the relay URL already.
The owner's phone generates the token and registers it with `share` before showing the QR.
The guest app treats the computer as a shared session: that one session, no folder browsing, no new sessions.

## 7. Relay frames

Documented in the module docstring of `aaw_core/relay/server.py` and summarized in `relay/README.md`.
The hello frame may carry `proto`, the client's wire protocol version (`PROTO_VERSION` in `aaw_core/transport/base.py`, currently 1), and the welcome echoes the relay's; a client that sends none counts as version 1.
Additive changes never bump it; a breaking change raises the relay's minimum, and the relay then refuses an outdated client with an actionable error instead of failing strangely.
The relay stores only the SHA-256 of device tokens (clients keep sending the raw token); a store or backup therefore contains no usable credentials.
The phone connects with `hello {role: "phone", token, platform?, push_token?}`, `subscribe`s to a session, `ack`s sequence numbers, sends `command` frames, and reads `history`, `commands`, `project`, `projects`, `computer`.
It may set `auto_approve` and `pending_message` on a project with `state` (other fields are dropped), and forget a session with `project_delete`.
It never writes events: the computer echoes a delivered prompt and a consumed answer into the feed as `message` events with `role: user`.
On unlink, or when the user removes the computer, it sends `forget_phone {req}` (answered by `forgotten`): the relay drops that token and its push token, so no push reaches the phone again.

Session sharing (relay store version 4, 0.1.13).
An unscoped phone mints a guest with `share {req, project_id, guest_token, label_enc?}` (the token is its own random value; `label_enc` is the owner's name for the guest, encrypted), lists them with `shares {req, project_id}` (without `project_id`: every guest on the computer, the owner's overview), and drops one with `revoke_share {req, project_id, share_id}`; each is answered with `shares {req, project_id?, shares[{id, project_id, label_enc, platform, created, last_seen}]}`, and a revoked guest's sockets are closed with `error {message: "access revoked"}`.
A guest's `welcome` carries `scope`, its project id.
The relay accepts from a guest only `ping`, `forget_phone`, `computer`, `projects` (filtered to its session), `subscribe`, `ack`, `history`, `commands`, `command`, `command_update`, `command_delete`, `project`, `state` and (0.1.18) `notify_pause` carrying its project id, and since 0.1.16 the file requests below; anything else, the session requests and `project_delete` included, is answered with `error {message: "not allowed for a shared session", req}`.
Pausing a shared session's notifications, since 0.1.18.
A phone, the owner's or a guest's, sets its own pause for one session with `notify_pause {req, project_id, paused: true | false}` and reads it by leaving `paused` out; both are answered with `notify_pause {req, project_id, paused}`.
The pause belongs to that phone token and that session, lasts until the phone resumes it, and goes when the token is unlinked or revoked or the session is deleted.
A relay before 0.1.18 answers `error {message: "unknown frame type 'notify_pause'"}` (with `req` only since 0.1.18, so a client should also treat a timeout as "the relay is too old").
While paused, the phone is notified only of the turns it started itself.
A turn belongs to whoever sent the prompt that started it, and the relay reads that from the feed: the last `message` with `role: user` and no `question_id` (an answer starts no turn).
With a `from` it is that guest's turn; with a `command_id` whose command carries `via: "phone"` and no `from` it is the owner's (every unscoped phone is the owner); typed at the computer, sent by the computer's own tools, or scheduled (`deliver_at`), it is nobody's, and everyone is notified whatever they paused.
For an event of somebody else's turn that would wake a phone (a `question`, a `notification` at `success`, `error` or `warning`), a paused phone gets no push, and its live sockets receive the `event` frame with `quiet: true`: the app shows it in the feed and posts no banner.
The same holds for a missed event replayed after `subscribe`.
The rule is the relay's alone; a phone only sets the pause and honors `quiet`.
A guest's files, since 0.1.16: a guest may send `request` with kind `fs_browse`, `fs_fetch` or `fs_attach`, written exactly as an owner's phone writes them.
The relay cannot judge a path (it crosses the relay encrypted), so the fence is the computer's: the relay forwards the request as kind `guest_<kind>` with one more field, `scope` (the guest's project id), under a request id of its own, and the supervisor serves `fs_browse` and `fs_fetch` with that session's folder as the only root.
The root listing is the folder itself (`at_root`, no parent), a path outside it or a link that leaves it answers `outside_roots`, and a scope with no known session or folder answers `denied`.
An engine older than 0.1.16 does not know the `guest_` kinds and answers `unknown_request`, so it never serves a guest the whole computer; the guest app should read that as "the computer needs an update".
The response returns to the guest that asked and to nobody else, under the id and kind the guest used; `start_session`, `stop_session` and `new_session` stay refused.
A file a phone attached lives in the computer's attachments folder, outside the session's folder, so a guest cannot open another phone's attachment from the feed.
It forwards a guest only its session's frames and the computer document, and pushes it only for its session.
Sharing is free on every plan and a guest keeps full control of the session (prompts, answers, stop and restart through commands); the owner cannot approve a guest's prompts before they run, but since 0.1.16 every one of them is named in the feed and to the agent (section 3).
A revoked guest keeps the key, so history it already fetched stays readable; key rotation is not part of this version.

The phone's `request` frames the supervisor answers (`response {req, kind, payload}`):

| kind | payload | response payload |
| --- | --- | --- |
| `start_session` | `project_id` | `result`: `started` \| `not_found` \| `error`; `session_id` |
| `stop_session` | `project_id` | `result`: `stopped` \| `not_found` |
| `new_session` | `path_enc`, `agent`, `model?`, `intent?` (`parallel`) | `result`: `started` \| `conflict` (+ `conflict_agents`, `conflict_session_id`) \| `outside_roots` \| `not_found` \| `not_a_dir` \| `agent_unavailable` \| `error`; `session_id` |
| `fs_browse` | `path_enc?` (absent = the first root) | `resolved_path_enc`, `parent_enc`, `at_root`, `entries[{name_enc, kind, is_repo, size?}]`, `truncated`, `error` |
| `fs_fetch` | `path_enc`, `parts?` (true: one response frame per chunk), `max_px?` (an image to look at: a JPEG rendition at most this wide or tall, when the original is over 300 KB and the computer can render one; `size` and `mime` describe the rendition) | `mime`, `size`, `total_chunks`, `chunks[<encrypted base64>]`, `error`; with `parts`, each frame carries one chunk and its 0-based `part`, and the phone joins them up to `total_chunks` (the relay caps a frame at 1 MiB; without `parts` the file cap is 4 MiB, with it 12 MiB). Text (`.md`, `.txt`, `.csv`) and images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.heic`) |
| `fs_attach` | A file for a prompt, written under the computer's attachments folder (swept after 24 h). In one frame: `suffix`, `chunks[<encrypted base64>]` (the phones up to 2.11 send a downscaled photo this way; the relay's 1 MiB frame cap limits it). In parts, since 0.1.12: `upload_id` (8 to 64 of `A-Za-z0-9_-`, the phone's own), `part` (0-based), `total_parts`, `suffix`, and exactly one chunk, a slice of at most 384 KiB before encoding; the phone sends the parts one request at a time and may resend one whose reply it lost, and `total_parts` and `suffix` must stay the same in every part. Allowed: images (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`, `.heic`), `.pdf`, video (`.mov`, `.mp4`, `.m4v`), and text and source files (`.txt`, `.md`, `.csv`, `.json`, `.log`, `.py`, `.swift`, `.kt`, and the like; `aaw_core.host.fs.ATTACH_SUFFIXES`); no archives, executables or `.svg` | In one frame: `path_enc`, `size`, `error`. In parts: `received` (the part's index, or -1 on an error), `size` (bytes staged so far), `error`, and on the reply that completes the file `path_enc`. The file cap is 12 MiB in one frame and 50 MiB in parts; an upload left incomplete for an hour is removed. `error`: `unsupported_type` \| `too_large` \| `not_a_file` \| `denied` |

When no computer socket is live the relay itself answers `{"error": "offline"}`.

The relay enforces limits on every client (the `Abuse limits` constants in `aaw_core/relay/server.py`).
Incoming frames are capped at 1 MiB and budgeted per connection; a socket must say hello within 10 s. Each computer keeps at most 64 MiB on the relay, event payloads and command documents together, and at most 500 unconsumed commands; past that a write is answered with `error: storage quota exceeded`. The store (version 3 since 0.1.9, version 4 since 0.1.13 with the guest columns) keys events and commands by computer and id, so two computers may use the same ids, and a migration from version 2 purges the plaintext `args`, `answer` and `options` that older writers stored next to their encrypted copies, after a rollback snapshot `<db>.pre-v3`.
A `history` response returns at most 1000 events and 8 MiB of payload, newest first, whatever `limit` asks.
Each computer may retain at most 64 MiB of event payload; an `event` beyond that is answered with `error {message: "storage quota exceeded"}` and not stored.
A fresh token cannot register a `computer_id` that is live or was seen in the last 30 days under another token; only a long-idle id (a wiped reinstall) rebinds.
Clients that hit a limit should back off, not retry in a loop.

## 8. Push

The relay forwards every event to the live phone sockets, then waits up to 4 s for each phone's `ack`; every registered phone token that did not ack (no socket, a socket left behind by a killed app, a dead network) is handed to the `PushSender`.
So a phone with the app on screen is never pushed, and a phone that only looks connected is.
The hosted relay uses `aaw_core/relay/push_fcm.py` (FCM HTTP v1, a service account given as configuration), which sends the same message the hosted product's Cloud Function sends today, so the apps and the iOS Notification Service Extension need no change:

- pushed: every `question`; a `notification` at `success`, `warning`, or `error`; nothing else.
  A permission question is not pushed while the project has `auto_approve` (a `choice` question always is).
- data: `computer_id`, `project_id`, `event_id`, `type`, `kind` (`permission` | `choice`), `agent`, `encrypted_body` (the encrypted question or message, or `""` when it would exceed FCM's payload cap; never truncated).
  There is no `uid`: a relay computer has none.
- iOS: a `notification` block with a generic title and body, `apns.payload.aps` with `sound`, `mutable-content: 1`, and `category: PERMISSION_PROMPT` for permission questions.
- Android: data only (`title` inside `data`), `android.priority: high`.
- A token FCM reports as unregistered is forgotten by the relay.
