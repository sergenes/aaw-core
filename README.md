# Agents At Work Core

**The open-source engine behind [Agents At Work](https://www.agentsatwork.app).**
Run Claude Code, Codex, Gemini CLI, Grok, Cursor, and Scoot on your own Mac or Linux box, and watch and steer them from your phone: approve tool calls, answer questions, send prompts, schedule prompts for later, from anywhere, end-to-end encrypted.

> **Status: pre-alpha, feature complete, not yet released.** The engine is ported: encryption, the relay, the transport, the hooks, the daemon, the host (`aaw`), and the login services, with an end-to-end test that runs the real daemon against a real relay and a real tmux session on every CI run (Linux and macOS). This repository stays private, and nothing is on PyPI, until the host has been driven with real agents and a real phone from a clean install on both platforms (`docker/README.md` is the Linux walkthrough).

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

On Linux, one command installs or updates everything (its own virtualenv, the `aaw` command,
the shell integration, linking with your phone, the service), and removes the earlier
Agents At Work Linux host if it finds one:

```bash
curl -fsSL https://agentsatwork.app/install-linux.sh | bash    # the same as scripts/install.sh here
```

Or by hand, on Linux or macOS:

```bash
pipx install aaw-core                      # Python 3.11+
aaw link                                   # asks which relay (ours, free, or your own), prints a QR code to scan
aaw service install                        # the always-on part, started at every login (or: aaw supervisor)
aaw shell-integration on                   # typing claude, codex, ... in a folder starts a bridged session
cd ~/my-project && claude                  # start an agent as you normally would
aaw status                                 # see it bridged; approve from your phone
```

## Desktop notifications

The phone is the primary channel, and the computer itself also gets a banner when a turn finishes, an error happens, or the agent waits for input: `osascript` on macOS, `notify-send` on a Linux desktop (GNOME, KDE, and the rest). A server or a box without a desktop simply has no `notify-send` and shows nothing. On macOS, while any Focus is on (Do Not Disturb included), banners from scripts go to Notification Center silently unless Agents At Work is allowed under System Settings > Focus > Allowed Apps; `aaw status` reminds you. Two keys in `~/.aaw/config.json`: `local_notifications` (default `true`; a GUI that shows its own banners turns it off) and `waiting_alert_seconds` (default `0`, off; the seconds a question may sit unanswered before a "waiting for your answer" banner, for when the push is muted while you are at the desk).

## Supported agents

Claude Code, Codex CLI, Gemini CLI, Grok CLI, Cursor, and [Scoot](https://github.com/sergenes/scootcli) (local models with Ollama, plus cloud models). Not affiliated with Anthropic, OpenAI, Google, xAI, Anysphere, or Ollama.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions use the DCO (`git commit -s`). Security reports: see [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
