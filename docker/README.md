# Testing the host on a clean Linux box

The end-to-end test (`tests/test_e2e_host.py`) already runs the real daemon against a real
relay and a real tmux session, with a fake echo agent, on every CI run.
This folder is for the manual pass with a real agent and a real phone, on Linux, from a clean install.

## 1. Build and start

From the repository root:

```bash
docker build -f docker/linux-host.Dockerfile -t aaw-linux-host .
docker run -it --rm --name aaw-host -p 8765:8765 aaw-linux-host
```

Inside the container, start a relay in the background and point the host at it:

```bash
python -m aaw_core.relay --host 0.0.0.0 --port 8765 --db /tmp/relay.sqlite > /tmp/relay.log 2>&1 &
export AAW_RELAY_URL=ws://127.0.0.1:8765/v1/ws
```

(For a phone on the same network, use the machine's LAN address in the QR: `export AAW_RELAY_URL=ws://<lan ip>:8765/v1/ws` before `aaw link`.
The phone app needs `wss://` in production; plain `ws://` is for a LAN test only.)

## 2. Link, run, and use it

```bash
aaw link                 # scan the QR with the phone app
aaw supervisor &         # or `aaw service install` where systemd --user exists
pip install -U scootcli  # a real, free agent to drive: scoot with an Ollama model, or any CLI agent you have
mkdir ~/proj && aaw start ~/proj --agent scoot --model ollama/qwen2.5:latest
```

Then from the phone: send a prompt, answer a permission prompt, schedule a prompt, browse a folder and start a session there, stop the session.
`aaw feed proj` and `aaw status` show the same from the terminal; `~/.aaw/logs/` has the daemon and supervisor logs.

## 3. Reset

`aaw uninstall --yes` removes the hooks, the service, and `~/.aaw`; the container is throwaway anyway.
