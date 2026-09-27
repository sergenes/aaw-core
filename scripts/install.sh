#!/usr/bin/env bash
# install.sh: install or update Agents At Work Core (the `aaw` command) on Linux.
#
#   curl -fsSL https://agentsatwork.app/install-linux.sh | bash
#   bash scripts/install.sh [--no-service] [--no-shell]
#
# Run it again at any time to update. What it does:
#   1. checks the prerequisites (python3 3.11+ with venv, a tmux whose capture-pane works);
#   2. removes the earlier Agents At Work Linux host if it finds one (its own
#      `aaw uninstall`), since both install a command called `aaw`;
#   3. installs aaw-core into its own virtualenv and links ~/.local/bin/aaw to it;
#   4. turns on the shell integration (typing claude, codex, ... in a folder starts a
#      bridged session), links this computer to your phone the first time (a QR code),
#      and installs the always-on service (systemd --user).
#
# AAW_CORE_SPEC  what pip installs (default: aaw-core from PyPI; a wheel path or URL works)
# AAW_CORE_HOME  where the virtualenv lives (default: ~/.local/share/aaw-core)
set -euo pipefail

SPEC="${AAW_CORE_SPEC:-aaw-core}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
HOME_DIR="${AAW_CORE_HOME:-$DATA/aaw-core}"
VENV="$HOME_DIR/venv"
BIN="$HOME/.local/bin"
OLD_DIR="$DATA/agentsatwork"
NO_SERVICE=0
NO_SHELL=0
for a in "$@"; do
  case "$a" in
    --no-service) NO_SERVICE=1 ;;
    --no-shell) NO_SHELL=1 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

if [[ "$(id -u)" == "0" ]]; then
  echo "Run this as your normal user, not root: aaw keeps its state and service in your home."
  exit 1
fi

# ── 1. prerequisites: everything missing reported at once, with one line to fix it ──
MISSING=()
command -v tmux >/dev/null || MISSING+=(tmux)
if ! command -v python3 >/dev/null; then
  MISSING+=(python3 python3-venv)
else
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "python3 3.11 or newer is required (found $(python3 --version 2>&1))"; exit 1; }
  # venv needs ensurepip, which Debian and Ubuntu ship in a separate package
  python3 -c 'import ensurepip' 2>/dev/null || MISSING+=(python3-venv)
fi
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "Missing prerequisites: ${MISSING[*]}"
  if command -v apt >/dev/null; then echo "  sudo apt install -y ${MISSING[*]}"; else echo "  sudo dnf install -y ${MISSING[*]}"; fi
  echo "then run the installer again."
  exit 1
fi

# aaw reads the agent's screen with `tmux capture-pane`. RHEL 10 and its rebuilds ship a
# 2023 git snapshot of tmux ("next-3.4") whose server aborts on the first capture-pane,
# taking every session with it. Probe on a private socket so the user's own tmux server
# is never touched, and refuse rather than install something that stops every session.
PROBE_SOCKET="aaw-probe-$$"
if tmux -L "$PROBE_SOCKET" new-session -d -s probe 'sleep 10' 2>/dev/null; then
  if ! tmux -L "$PROBE_SOCKET" capture-pane -p -t '=probe:' >/dev/null 2>&1; then
    tmux -L "$PROBE_SOCKET" kill-server 2>/dev/null || true
    echo "This tmux build ($(tmux -V)) crashes on capture-pane, which aaw relies on."
    echo "Install tmux 3.4 or newer, then run the installer again. From source:"
    echo "  sudo dnf install -y gcc make libevent-devel ncurses-devel bison"
    echo "  curl -fsSLO https://github.com/tmux/tmux/releases/download/3.5a/tmux-3.5a.tar.gz"
    echo "  tar xzf tmux-3.5a.tar.gz && cd tmux-3.5a && ./configure && make -j && sudo make install"
    exit 1
  fi
  tmux -L "$PROBE_SOCKET" kill-server 2>/dev/null || true
fi

# ── 2. the earlier Agents At Work Linux host ─────────────────────────────────────────
# It lived in ~/.local/share/agentsatwork with state in ~/.agent-bridge, a systemd unit
# agentsatwork-supervisor and its own ~/.local/bin/aaw. Its uninstall stops its sessions,
# strips its hooks and shell line, and removes its files; its launcher it left behind.
if [[ -f "$OLD_DIR/linux/aaw.py" && -x "$OLD_DIR/run_python.sh" ]]; then
  echo "→ Removing the earlier Agents At Work Linux host (its sessions stop; start them again after)"
  "$OLD_DIR/run_python.sh" "$OLD_DIR/linux/aaw.py" uninstall --yes || echo "  its uninstall reported a problem; cleaning up what is left"
fi
if command -v systemctl >/dev/null && [[ -f "$HOME/.config/systemd/user/agentsatwork-supervisor.service" ]]; then
  systemctl --user disable --now agentsatwork-supervisor >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/agentsatwork-supervisor.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi
if [[ -e "$BIN/aaw" || -L "$BIN/aaw" ]] && grep -qs "agentsatwork/run_python.sh" "$BIN/aaw"; then
  rm -f "$BIN/aaw"
fi

# ── 3. aaw-core in its own virtualenv ────────────────────────────────────────────────
echo "→ Installing aaw-core into $VENV"
mkdir -p "$HOME_DIR" "$BIN"
# A venv left without pip by an earlier failed run is rebuilt.
if [[ ! -x "$VENV/bin/pip" ]]; then
  rm -rf "$VENV"
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "$SPEC"
# A wheel file or URL may carry the same version as the installed one (a development
# build); --upgrade keeps the installed copy then, so reinstall the package itself.
case "$SPEC" in
  *.whl|*/*) "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$SPEC" ;;
esac
ln -sfn "$VENV/bin/aaw" "$BIN/aaw"
AAW="$BIN/aaw"
echo "  $("$AAW" --version)"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "  note: $BIN is not on your PATH; add it in your shell rc to type \`aaw\`" ;;
esac

# ── 4. shell integration, link, service ──────────────────────────────────────────────
if [[ "$NO_SHELL" == "0" ]]; then
  "$AAW" shell-integration on | sed 's/^/  /'
fi

STATE="${AAW_STATE_DIR:-$HOME/.aaw}"
if [[ ! -f "$STATE/host.json" ]]; then
  if [[ -r /dev/tty ]] && { : </dev/tty; } 2>/dev/null; then
    echo "→ Linking this computer to your phone"
    "$AAW" link </dev/tty
  else
    echo "→ Not linked yet: run \`aaw link\` and scan the QR code with the Agents At Work app"
  fi
fi

if [[ "$NO_SERVICE" == "0" ]]; then
  if [[ -f "$STATE/host.json" ]]; then
    echo "→ Installing the always-on service"
    "$AAW" service install | sed 's/^/  /'
  else
    echo "→ After \`aaw link\`, run \`aaw service install\` for the always-on service"
  fi
fi

echo
echo "Done. Start an agent in a project folder (e.g. cd ~/my-app && claude), or from the phone."
echo "Update any time by running this installer again."
