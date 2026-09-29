# Agents At Work Core

[![PyPI](https://img.shields.io/pypi/v/aaw-core.svg?label=PyPI)](https://pypi.org/project/aaw-core/)
[![tests](https://github.com/sergenes/aaw-core/actions/workflows/ci.yml/badge.svg)](https://github.com/sergenes/aaw-core/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#install)
[![Agents](https://img.shields.io/badge/agents-Claude%20Code%20%7C%20Codex%20%7C%20Gemini%20%7C%20Grok%20%7C%20Cursor%20%7C%20scoot-orange.svg)](#supported-agents)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

What changed in each version: [GitHub Releases](https://github.com/sergenes/aaw-core/releases).

**The open-source engine behind [Agents At Work](https://agentsatwork.app).**
Run Claude Code, Codex, Gemini CLI, Grok, Cursor, and scoot on your own Mac or Linux box, and follow them from your phone: approve tool calls, answer questions, and send and schedule prompts, end-to-end encrypted.

> **Status: beta.**
> This is the engine behind the shipping Agents At Work apps: the Mac app bundles it, and the hosted relay runs it.
> The wire protocol with the phone apps is stable ([the contract](./docs/protocol.contract.md)); the CLI flags and config keys can still change between 0.x releases.

## What is open and what is not

`aaw-core` is everything that runs on *your* computer: the daemon that watches your agents in `tmux`, the hooks that report what they do, and the `aaw` command line.
It also contains the relay server that connects your computer to your phone.
All of it is MIT licensed.

The phone apps (iPhone, iPad, Android) and the Mac menu-bar app are not in this repository.
The Mac app is a free download that runs this same engine.
The phone apps are free for one computer on our hosted relay; the Personal and Pro plans raise that to 3 and 15 computers.
Computers on a relay you host yourself never count toward a plan.

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

## Docs

- [Architecture](./docs/architecture.md): the daemon, the hooks, the store, and the relay, and how a message travels.
- [The protocol contract](./docs/protocol.contract.md): encryption, events, commands, relay frames, push, and the limits the relay enforces.
- [Hosting the relay yourself](./relay/README.md): docker compose, TLS, and backups.
- [Contributing](./CONTRIBUTING.md): DCO sign-off, the PR flow, and how releases are cut.

## Install

On Linux, one command installs or updates everything: its own virtualenv, the `aaw` command, the shell integration, the link with your phone (a QR code), and the always-on service.
It also removes the earlier Agents At Work Linux host if it finds one.

```bash
curl -fsSL https://agentsatwork.app/install-linux.sh | bash    # the same script as scripts/install.sh
```

Or by hand, on Linux or macOS (on a Mac, the free Agents At Work app does all of this for you):

```bash
pipx install aaw-core                      # Python 3.11+, and tmux
aaw link                                   # asks which relay (ours, or your own), prints a QR code to scan
aaw service install                        # the always-on part: runs in the background and starts at every login
aaw shell-integration on                   # typing claude, codex, ... in a folder starts a bridged session
```

Then start an agent the way you always do (`cd ~/my-project && claude`), or start one from the phone.

Coming from the earlier Agents At Work Linux tool by hand: its `aaw uninstall` leaves its `~/.local/bin/aaw` launcher behind, and pipx will not replace a file it does not own, so `aaw` keeps running the old launcher ("run_python.sh: No such file or directory").
Remove it first with `rm ~/.local/bin/aaw`, then `pipx install --force aaw-core`.
The one-line installer handles this for you.

## Everyday commands

```bash
aaw status                          # the computer, the relay, and every session
aaw start ~/my-project              # start a session (--agent codex, gemini, grok, cursor, scoot)
aaw stop my-project                 # stop one session
aaw feed my-project                 # the conversation, paged; -f follows it live
aaw send my-project "run the tests" # send a prompt
aaw schedule my-project --at 2am "continue with the refactor"   # a prompt for later
aaw mobile-mode on                  # permission prompts go to the phone
aaw quit                            # stop everything; the next login (or aaw service start) brings it back
aaw uninstall                       # remove the service, the hooks, the shell line, and ~/.aaw
```

`aaw --help` lists every command, and `aaw <command> --help` its options.

## Running on a laptop

A sleeping computer cannot run agents or reach the phone.
While a session runs, the engine stops the computer from sleeping on its own when idle (`caffeinate` on macOS, `systemd-inhibit` on Linux), but it cannot overrule a closed lid.
A MacBook on battery always sleeps when the lid closes, and most Linux laptops suspend on lid close whatever inhibitors are held.
While it sleeps, the phone shows the computer as offline, and a message sent during one of its short background wakes may not reach the agent: the phone says so, and you can send it again once the computer is awake.

To keep agents working while you are away from the laptop:

- Leave the lid open. Turn the brightness down if you like: the computer keeps running.
- On a Mac, use closed-display mode: connect power and an external display (plus a keyboard and mouse), then close the lid.
- On a Mac that must run with the lid closed and no display, `sudo pmset -a disablesleep 1` turns sleep off completely, on battery too, and `sudo pmset -a disablesleep 0` turns it back on. Remember to turn it back on: a Mac that cannot sleep drains its battery and gets hot in a bag.
- On a Linux laptop, set `HandleLidSwitch=ignore` and `HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf` and reboot. Some desktops (GNOME, KDE) have their own lid setting that also has to allow it.

## Hosting the relay yourself

The relay is a small server in this repository (`aaw_core/relay/`): run it with `docker compose` from `relay/`, or with `pip install "aaw-core[relay]"` and `python -m aaw_core.relay`, behind a TLS proxy.
Point your computer at it with `aaw link --relay wss://your.host/v1/ws`.
It sees only ciphertext and routing metadata, the same as ours, and it enforces per-client limits (frame size and rate, storage and push budgets; see the protocol contract).
One difference: waking the phone with a push notification needs the app's own push credentials, so on your own relay the phone gets updates while the app is open, not in the background.
`relay/README.md` has the details.

## Desktop notifications

The phone is the primary channel, and the computer itself also gets a banner when a turn finishes, an error happens, or the agent waits for input: `osascript` on macOS, `notify-send` on a Linux desktop (GNOME, KDE, and the rest). A server or a box without a desktop simply has no `notify-send` and shows nothing. On macOS, while any Focus is on (Do Not Disturb included), banners from scripts go to Notification Center silently unless Agents At Work is allowed under System Settings > Focus > Allowed Apps; `aaw status` reminds you. Two keys in `~/.aaw/config.json`: `local_notifications` (default `true`; a GUI that shows its own banners turns it off) and `waiting_alert_seconds` (default `0`, off; the seconds a question may sit unanswered before a "waiting for your answer" banner, for when the push is muted while you are at the desk).

## Supported agents

Claude Code, Codex CLI, Gemini CLI, Grok CLI, Cursor, and [Scoot](https://github.com/sergenes/scootcli) (local models with Ollama, plus cloud models). Not affiliated with Anthropic, OpenAI, Google, xAI, Anysphere, or Ollama.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions use the DCO (`git commit -s`). Security reports: see [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
