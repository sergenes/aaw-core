# Agents At Work Core

**The open-source engine behind [Agents At Work](https://www.agentsatwork.app).**
Run Claude Code, Codex, Gemini CLI, Grok, Cursor, and Scoot on your own Mac or Linux box, and watch and steer them from your phone: approve tool calls, answer questions, send prompts, schedule prompts for later, from anywhere, end-to-end encrypted.

> **Status: pre-alpha, being built.** This repository is private while the engine is assembled and tested end to end. It becomes public, with a first release on PyPI, only once the host runs against the relay on Linux and macOS with real agents from a clean install.

## What this is, and what it is not

`aaw-core` is the **headless host** (the daemon that watches your agents in `tmux`, the hooks, the `aaw` command line) plus the **relay** that connects it to your phone.
It is everything that runs on *your* computer, and it is MIT licensed.

It is **not** the apps. The iPhone, Android, and macOS apps are the paid convenience and stay closed. The free path is: run `aaw-core` on your computer, install the phone app from the store, scan a QR code, done. If you want the Mac menu-bar app, hosted sync, or several computers, that is the subscription.

## How it works

```
your computer                                          your phone
  agent in tmux  <->  aaw daemon  <->  local store        store app
                          |                                  ^
                          | one outbound WebSocket           |
                          v                                  |
                     the relay (tiny; ciphertext + routing only) -> push (FCM/APNs)
```

- Your **local store** on the computer is the source of truth for the conversation history.
- The daemon keeps **one outbound WebSocket** to a relay, so it works behind any home router or cellular NAT with **no ports, no tunnel, no VPN, no Firebase project**.
- The relay buffers what is in flight while the phone is asleep and triggers a wake-up push. It only ever sees **ciphertext and routing metadata**: prompts, answers, and content are AES-256-GCM encrypted with a key that only your computer and your phone hold, exchanged once by QR.
- We host a relay for free users; self-hosters run the same server with `docker compose`.

## Quickstart (once released)

```bash
pipx install aaw-core                      # Linux or macOS, Python 3.11+
export AAW_RELAY_URL=wss://relay.example/v1/ws   # the hosted relay, or your own (relay/README.md)
aaw link                                   # prints a QR code: scan it with the phone app
aaw supervisor                             # the always-on part (a user service does this after install)
cd ~/my-project && claude                  # start an agent as you normally would
aaw status                                 # see it bridged; approve from your phone
```

## Supported agents

Claude Code, Codex CLI, Gemini CLI, Grok CLI, Cursor, and [Scoot](https://github.com/sergenes/scootcli) (local models with Ollama, plus cloud models). Not affiliated with Anthropic, OpenAI, Google, xAI, Anysphere, or Ollama.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions use the DCO (`git commit -s`). Security reports: see [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
