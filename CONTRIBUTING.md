# Contributing to Agents At Work Core

Thanks for helping. This is the open-source engine behind Agents At Work; the apps are closed, but everything that runs on your computer lives here.

## Developer Certificate of Origin (DCO)

Every commit must be signed off:

```bash
git commit -s -m "your message"
```

That adds a `Signed-off-by: Your Name <you@example.com>` trailer, certifying you have the right to contribute the change under the MIT license (the [DCO 1.1](https://developercertificate.org/)). A DCO check runs on every pull request; unsigned commits fail it. There is no CLA.

## Development setup

```bash
git clone https://github.com/sergenes/aaw-core
cd aaw-core
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,relay]"
pytest
ruff check .
```

Python 3.11+. The host runs on Linux and macOS; the relay runs anywhere Python does.

## What gets extra scrutiny

Changes to these paths are reviewed more slowly and carefully, because they affect security or compatibility with the closed phone apps:

- `aaw_core/encryption.py`: the AES-256-GCM envelope must stay byte-compatible with the apps.
- Pairing and the QR payload.
- `aaw_core/transport/` and `relay/`: event and command shapes are a contract with the apps (see `docs/protocol.contract.md`).

## Before opening a pull request

- For anything non-trivial, open an issue or discussion first so you do not spend a weekend on something that conflicts with the roadmap.
- CI must be green: build, tests, lint, and the secret scanner.
- Never commit secrets, keys, tokens, or anything pointing at a specific hosted project. Configuration comes from environment variables and `~/.aaw/config.json`; the code has no hosted defaults.

## How this project is developed

The engine is developed here, in the open. It was assembled by porting the host from the maintainer's private product, and it is the intended engine for that product going forward, so good contributions here reach both.
