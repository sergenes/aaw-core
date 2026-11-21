"""aaw: the Agents At Work Core command line.

  aaw link                          pair a phone: choose the relay (first run), show the QR code
  aaw start DIR [--agent A]         start an agent on a folder and attach to its tmux session
  aaw stop [ID | --all]             stop one session, or every session
  aaw status                        sessions, daemons, supervisor, pairing
  aaw feed ID [-f | --remote]       a session's conversation, paged (tmux panes do not scroll)
  aaw send ID TEXT                  type a prompt into a running session
  aaw schedule ID TEXT --at WHEN    queue a prompt for later
  aaw scheduled ID [...]            list, edit or delete scheduled prompts
  aaw mobile-mode on|off|status     route permission prompts to the phone
  aaw supervisor                    run the always-on part in the foreground
  aaw service install               ... or as a login service (systemd --user / launchd)
  aaw quit | uninstall              turn the host off / remove it from this machine
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aaw_core import __version__
from aaw_core.config import HOSTED_RELAY_URL, Settings, load_settings, save_config
from aaw_core.encryption import load_or_create_key
from aaw_core.hooks.common import read_key
from aaw_core.host import hooks_installer, service, sessions, state
from aaw_core.host.identity import load_identity, load_or_create_identity
from aaw_core.host.session_id import resolve
from aaw_core.transport.relay import RelayTransport

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
QR_VERSION = 1


TMUX_MISSING = ("aaw: tmux is not installed, and every session runs inside it. Install it, then run this again:\n"
                "  macOS:          brew install tmux\n"
                "  Debian, Ubuntu: sudo apt install tmux\n"
                "  Fedora, RHEL:   sudo dnf install tmux")


def die(msg: str, code: int = 1) -> None:
    print(f"aaw: {msg}", file=sys.stderr)
    sys.exit(code)


# ── relay access ─────────────────────────────────────────────────────────────


def _transport(settings: Settings, project: str, *, wait: float = 10.0) -> RelayTransport:
    """A connected transport for one project's channel; dies with a hint when not linked."""
    if not settings.relay_url:
        die("no relay configured. Set AAW_RELAY_URL (or relay_url in ~/.aaw/config.json), then `aaw link`.")
    ident = load_identity(settings)
    if ident is None:
        die("this computer is not linked yet. Run `aaw link` first.")
    t = RelayTransport(relay_url=settings.relay_url, token=ident.token, computer_id=ident.computer_id,
                       project_id=project, sessions_dir=settings.sessions_dir,
                       computer_name=ident.computer_name, enc_key=read_key(settings)).start()
    if not t.wait_connected(wait):
        t.stop(flush_timeout=0)
        die(f"could not reach the relay at {settings.relay_url}")
    return t


def _known(settings: Settings):
    """Live tmux sessions plus the relay's stopped project documents, for the id resolver."""
    docs: dict = {}
    if settings.relay_url and load_identity(settings) is not None:
        t = None
        try:
            t = _transport(settings, "_aaw", wait=5)
            docs = t.list_projects()
        except SystemExit:
            print("(relay not reachable; resolving the session id from tmux alone)", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - the resolver still works from live tmux state
            print(f"(could not read the project documents: {e})", file=sys.stderr)
        finally:
            if t is not None:
                t.stop(flush_timeout=0)
    key = read_key(settings)

    def decrypt(value: str) -> str:
        from aaw_core.encryption import decrypt_if_encrypted, is_encrypted
        if is_encrypted(value):
            return decrypt_if_encrypted(value, key) if key else ""
        return value

    return sessions.known_sessions(docs, decrypt)


# ── link ─────────────────────────────────────────────────────────────────────


def qr_payload(settings: Settings, phone_token: str) -> str:
    ident = load_or_create_identity(settings)
    key = load_or_create_key(settings.session_key_file)
    return json.dumps({"v": QR_VERSION, "computer_id": ident.computer_id, "name": ident.computer_name,
                       "relay_url": settings.relay_url, "key": key, "token": phone_token},
                      sort_keys=True, separators=(",", ":"))


def deep_link(payload: str) -> str:
    """The same pairing payload as a tappable URL, for pairing from an SSH app on the
    phone itself (no camera to scan the QR). The payload (which contains the key and a
    slash-and-plus base64) is url-safe-base64'd into one query value so nothing in it
    needs escaping; the app decodes `d` and feeds it into the same pairing path as a scan.
    The key still travels phone-app to phone-app only, never through a server."""
    enc = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"agentsatwork://link?d={enc}"


def configure_relay(settings: Settings, *, url: str | None = None, ask=input, quiet: bool = False) -> Settings:
    """Pick the relay this computer uses and save it to config.json. With no `url`, ask:
    the hosted relay (free, notifications work) or the user's own."""
    if url is None:
        if not sys.stdin.isatty():
            die("no relay configured. Run `aaw link` in a terminal to choose one, pass --relay URL, "
                "or set relay_url in ~/.aaw/config.json (AAW_RELAY_URL also works).")
        print("Which relay should this computer use to reach your phone?\n"
              f"  [1] the Agents At Work relay ({HOSTED_RELAY_URL}): free, hosted by us, notifications work\n"
              "  [2] your own relay (see relay/README.md; no notifications unless you add a push sender)")
        choice = ask("Choice [1]: ").strip() or "1"
        if choice == "1":
            url = HOSTED_RELAY_URL
        elif choice == "2":
            url = ask("Relay URL (wss://your.host/v1/ws): ").strip()
        else:
            die("choose 1 or 2")
    if not url.startswith(("wss://", "ws://")):
        die(f"a relay URL starts with wss:// (ws:// only for a LAN test): {url!r}")
    path = save_config(settings.state_dir, relay_url=url)
    if not quiet:
        print(f"Relay saved to {path}: {url}")
    return load_settings()


def cmd_link(a, settings: Settings) -> None:
    if a.json and not a.relay and not settings.relay_url:
        # Non-interactive (a GUI is asking): the hosted relay unless one was chosen. Quiet:
        # with --json the payload is the whole of stdout.
        settings = configure_relay(settings, url=HOSTED_RELAY_URL, quiet=True)
    elif a.relay or not settings.relay_url:
        settings = configure_relay(settings, url=a.relay)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    ident = load_or_create_identity(settings)
    load_or_create_key(settings.session_key_file)
    t = _transport(settings, "_aaw")
    try:
        phone_token = t.register_phone_token()
        if not t.flush(10):
            die("the relay did not accept the phone token; try again")
    finally:
        t.stop(flush_timeout=0)
    payload = qr_payload(settings, phone_token)
    if a.json:
        print(payload)  # the payload only; the caller renders the QR
        return
    try:
        import qrcode
    except ImportError:
        die("the qrcode package is missing (pip install qrcode)")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    needed = len(qr.get_matrix()) + 4
    columns = shutil.get_terminal_size((80, 24)).columns
    if columns < needed and not a.force:
        print(f"This QR needs {needed} columns and the terminal has {columns}: it would wrap and not scan.\n"
              "Widen the window or zoom out, then run `aaw link` again (--force prints it anyway).")
        return
    print(f"\nComputer '{ident.computer_name}' ({ident.computer_id[:8]}...) via {settings.relay_url}\n")
    # Half blocks: one module per column, two per row, which is square in a terminal cell.
    # Inverted so light modules are the bright blocks on a dark terminal; --light otherwise.
    qr.print_ascii(invert=not a.light)
    print("\nScan with the Agents At Work app (Computers, then the QR icon). Each `aaw link` mints a new phone token;"
          " earlier ones keep working.")
    print("\nOr, if you are reading this in an SSH app on the phone itself, tap this link to pair without the camera:")
    print(f"  {deep_link(payload)}")
    if service.is_running():
        print("Then: aaw start ~/your/project")
    else:
        print("Then: aaw service install     # the always-on part: runs in the background, starts at every login")
        print("      aaw start ~/your/project")
    if a.show_payload:
        print(payload)


# ── sessions ─────────────────────────────────────────────────────────────────


def _installed_cli_agents() -> list[str]:
    return [x for x in sessions.AGENTS if hooks_installer.is_installed(x)]


def _pick_agent(requested: str | None) -> str:
    """With no --agent: the single installed agent, or Claude when several are installed."""
    installed = _installed_cli_agents()
    if not installed:
        die("no agent CLI found on this machine (looked for: " + ", ".join(sessions.AGENTS) + ").")
    if requested:
        if requested not in sessions.AGENTS:
            die(f"agent must be one of {', '.join(sessions.AGENTS)}")
        if requested not in installed:
            die(f"{requested} is not installed on this machine. Installed: {', '.join(installed)}.")
        return requested
    if len(installed) == 1:
        return installed[0]
    return "claude" if "claude" in installed else installed[0]


def _attach(project: str, a) -> None:
    if a.porcelain:
        print(project)
        return
    cmd = sessions.attach_command(project)
    if a.no_attach or not sys.stdout.isatty():
        print(f"Attach with: {cmd}")
        return
    os.execvp("tmux", cmd.split())


def cmd_start(a, settings: Settings) -> None:
    path = Path(a.dir).expanduser().resolve()
    if not path.is_dir():
        die(f"no such folder: {path}")
    agent = _pick_agent(a.agent)
    if a.model and agent != "scoot":
        die("--model is for --agent scoot")
    model = a.model
    if agent == "scoot":
        models = sessions.scoot_models()
        if models is None:
            die("scoot is not installed. Install it with: pipx install scootcli")
        from aaw_core.host.detect import has_scoot_history
        if not model and not has_scoot_history(path):
            hint = next((m for m in models if m.startswith("ollama/")), models[0] if models else "ollama/<model>")
            die("--model is required the first time on a folder (provider/model). Try `aaw models`.\n"
                f"e.g. aaw start {a.dir} --agent scoot --model {hint}")
        # An Ollama model is checked by start_session, which starts Ollama when it is down.
        if model and models and model not in models and not model.startswith("ollama/"):
            die(f"model {model} is not available. Run `aaw models` to see the list.")
    if a.id:
        project = a.id
    else:
        res = resolve(str(path), agent, _known(settings))
        if res.action == "attach":
            if not a.porcelain:
                print(f"{agent} is already running on this folder as {res.id}.")
            _attach(res.id, a)
            return
        if res.alongside and not a.yes and not a.porcelain:
            names = ", ".join((k.agent or "?").capitalize() for k in res.alongside)
            ans = input(f"'{path.name}' is already running with {names}. Open {agent.capitalize()} alongside "
                        f"as {res.id}? [Y/n] ")
            if ans.strip().lower() not in ("", "y", "yes"):
                return
        project = res.id
    try:
        lines = sessions.start_session(settings, path, project, agent, model=model)
    except sessions.SessionError as e:
        die(f"start failed: {e}")
    if not a.porcelain:
        print("\n".join(lines))
    _attach(project, a)


def _stop_all(settings: Settings) -> int:
    n = 0
    for s in sessions.list_sessions():
        sessions.stop_session(settings, s.project)
        _write_stopped(settings, s.project)
        n += 1
    return n


def cmd_stop(a, settings: Settings) -> None:
    if a.all or not a.id:
        n = _stop_all(settings)
        print(f"Stopped {n} session{'s' if n != 1 else ''}." if n else "No sessions were running.")
        return
    if not sessions.session_alive(a.id) and not sessions.daemon_pid(settings, a.id):
        die(f"no running session {a.id}")
    print("\n".join(sessions.stop_session(settings, a.id)))
    _write_stopped(settings, a.id)


def _write_stopped(settings: Settings, project: str) -> None:
    """Tell the phone right away. The daemon writes the same when it sees its tmux session
    go, but a stop kills the daemon a moment later and may win that race."""
    if not settings.relay_url or load_identity(settings) is None:
        return
    try:
        t = _transport(settings, project, wait=5)
    except SystemExit:
        return
    try:
        t.set_project_status("stopped", pending_question_id="")
        t.flush(5)
    finally:
        t.stop(flush_timeout=0)


# macOS delivers a script's banner to Notification Center only, silently, while any Focus
# (including Do Not Disturb) is on, unless the app is allowed in that Focus.
FOCUS_HINT = "; with a Focus on, allow Agents At Work under System Settings > Focus > Allowed Apps"


def status_snapshot(settings: Settings) -> dict:
    """Everything a local GUI shows, in one read: live sessions, the mirrored project
    documents, and the host's state. Cheap: tmux, pid files, small files."""
    ident = load_identity(settings)
    mm = settings.mobile_mode_file.read_text().strip() if settings.mobile_mode_file.exists() else ""
    try:
        enabled_at = int(settings.enabled_flag.read_text().strip() or 0)
    except (OSError, ValueError):
        enabled_at = 0
    live = []
    for s in sessions.list_sessions():
        pid = sessions.daemon_pid(settings, s.project)
        live.append({"id": s.project, "agent": sessions.session_agent(s.project) or "", "path": s.path,
                     "daemon_pid": pid or 0})
    return {
        "sessions": live,
        "projects": state.read_projects(state.mirror_dir(settings.state_dir)),
        "supervisor": {"enabled": settings.enabled_flag.exists(), "enabled_at": enabled_at,
                       "fresh": (time.time() - enabled_at) < 180 if enabled_at else False},
        "mobile_mode": "manual" if mm == "manual" else ("auto" if mm else "off"),
        "relay_url": settings.relay_url or "",
        "linked": ident is not None,
        "computer_id": ident.computer_id if ident else "",
        "computer_name": ident.computer_name if ident else settings.computer_name,
        "state_dir": str(settings.state_dir),
        "version": __version__,
        # 0.1.23: whether agents may propose notes and messages (`aaw peers on`), and
        # where the shared notes live.
        "peers": settings.peers,
        "knowledge_dir": str(settings.state_dir / "knowledge"),
    }


def cmd_status(a, settings: Settings) -> None:
    if a.json:
        snap = status_snapshot(settings)
        if a.agents:
            snap["detected_agents"] = hooks_installer.detected_agents()
            scoot = sessions.scoot_status()
            snap["scoot_models"] = scoot[0] if scoot is not None else None
            # Why a provider has no models (0.1.18), so an app can say it next to a scoot
            # it cannot start: {} when all answered, null when scoot is not installed.
            snap["scoot_errors"] = scoot[1] if scoot is not None else None
            # A newer scoot on PyPI (0.1.21): {installed, latest, command}, else null.
            snap["scoot_update"] = sessions.scoot_update(settings)
        print(json.dumps(snap, sort_keys=True))
        return
    live = sessions.list_sessions()
    print(f"Sessions ({len(live)}):")
    for s in live:
        pid = sessions.daemon_pid(settings, s.project)
        daemon = f"pid {pid}" if pid else "MISSING"
        print(f"  {s.project:24s} {sessions.session_agent(s.project) or '?':8s} daemon={daemon:12s} {s.path}")
    ident = load_identity(settings)
    mm = settings.mobile_mode_file.read_text().strip() if settings.mobile_mode_file.exists() else ""
    print(f"Supervisor: {'running (enabled flag present)' if settings.enabled_flag.exists() else 'not running'}")
    print(f"Mobile mode: {'on (manual)' if mm == 'manual' else ('on (recent phone activity)' if mm else 'off')}")
    print(f"Relay: {settings.relay_url or 'not configured (AAW_RELAY_URL)'}")
    print(f"Desktop banners: {'on' if settings.local_notifications else 'off (local_notifications)'}"
          + (FOCUS_HINT if settings.local_notifications and sys.platform == "darwin" else ""))
    print(f"Linked: {'yes, computer ' + ident.computer_id[:8] + '...' if ident else 'no (aaw link)'}; "
          f"session key: {'yes' if settings.session_key_file.exists() else 'no'}")
    scoot = sessions.scoot_status()
    if scoot is not None:
        print(f"Scoot: {len(scoot[0])} models available (aaw models)")
        for provider, why in sorted(scoot[1].items()):
            print(f"  {provider}: {why}")
        update = sessions.scoot_update(settings)
        if update:
            print(f"  scoot {update['latest']} is available (installed: {update['installed']}). "
                  f"Update with: {update['command']}")


# ── the feed ─────────────────────────────────────────────────────────────────


def render_feed(entries: list, width: int, tty: bool) -> str:
    """A colored header per turn and the body wrapped with a hanging indent: user prompts
    cyan, agent replies green, questions yellow, notes dim."""
    indent = "  "
    role_color = {"user": "36", "assistant": "32"}
    lines: list[str] = []

    def color(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if tty else s

    def block(header: str, body: str, code: str = "") -> None:
        lines.append("")
        lines.append(color(code, header) if code else header)
        for para in (body.split("\n") if body else [""]):
            if not para.strip():
                lines.append("")
                continue
            for wrapped in textwrap.wrap(para, width=width - len(indent),
                                         break_long_words=False, break_on_hyphens=False) or [""]:
                lines.append(indent + wrapped)

    for e in entries:
        ts = datetime.fromtimestamp((e.get("ts") or 0) / 1000, tz=UTC).astimezone().strftime("%H:%M:%S")
        t = e.get("type")
        if t == "message":
            role = e.get("role", "") or "?"
            block(f"[{ts}] {role}", e.get("content", ""), role_color.get(role, ""))
        elif t == "question":
            body = e.get("question", "")
            if e.get("options"):
                body += "\noptions: " + ", ".join(str(o) for o in e["options"])
            block(f"[{ts}] QUESTION", body, "33")
        elif t == "notification":
            block(f"[{ts}] note", e.get("message", ""), "2")
    return "\n".join(lines)


def _page(text: str, tty: bool) -> None:
    """Through a pager (scrolls, `/` searches) when writing to a terminal; tmux alternate-screen
    panes have no scrollback, so this is how a long feed stays readable."""
    if not tty or not text.strip():
        print(text)
        return
    pager = os.environ.get("PAGER") or ("less" if shutil.which("less") else "")
    if not pager:
        print(text)
        return
    args = pager.split()
    if os.path.basename(args[0]).startswith("less"):
        args += ["-R", "-F", "-X"]
    try:
        p = subprocess.Popen(args, stdin=subprocess.PIPE)
        p.communicate(text.encode(errors="replace"))
    except BrokenPipeError:
        pass
    except OSError:
        print(text)


def cmd_feed(a, settings: Settings) -> None:
    tty = sys.stdout.isatty()
    width = max(40, shutil.get_terminal_size((100, 24)).columns)
    if a.remote:
        if a.follow:
            print("note: --follow is not supported with --remote (snapshot only)", file=sys.stderr)
        t = _transport(settings, a.id)
        try:
            entries = t.read_events(limit=None if a.all else a.limit)
        finally:
            t.stop(flush_timeout=0)
        if not entries:
            die(f"no events on the relay for {a.id}")
        _page(render_feed(entries, width, tty), tty)
        return
    path = settings.sessions_dir / f"{a.id}.jsonl"
    if not path.exists():
        die(f"no local feed for {a.id} (is the session running? `aaw feed {a.id} --remote` reads the relay)")

    def parse(line: str):
        try:
            return json.loads(line)
        except ValueError:
            return None

    with path.open() as f:
        entries = [e for e in (parse(line) for line in f) if e is not None]
        if not a.follow:
            _page(render_feed(entries, width, tty), tty)
            return
        print(render_feed(entries, width, tty))
        while True:
            line = f.readline()
            if line:
                e = parse(line)
                if e is not None:
                    print(render_feed([e], width, tty))
            else:
                time.sleep(1)


# ── prompts: now and later ───────────────────────────────────────────────────


def parse_time_of_day(s: str) -> tuple[int, int] | None:
    """(hour, minute) from `22:00`, `8:00`, `10pm`, `10:30 PM`, `12am`; None otherwise.
    A bare number with no colon and no am/pm (`8`) is ambiguous and rejected."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", s.strip(), re.IGNORECASE)
    if not m:
        return None
    has_colon, ap = m.group(2) is not None, m.group(3)
    if not has_colon and not ap:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if ap:
        if not (1 <= hh <= 12) or mm > 59:
            return None
        ap = ap[0].lower()
        hh = (0 if hh == 12 else hh) if ap == "a" else (12 if hh == 12 else hh + 12)
    elif hh > 23 or mm > 59:
        return None
    return (hh, mm)


def parse_when(s: str, now: datetime | None = None) -> int:
    """A delivery time as epoch ms on this computer's clock: a time of day (next occurrence),
    `+3h` / `+90m`, or an absolute `YYYY-MM-DD HH:MM` (24-hour or 12-hour). Raises ValueError."""
    s = s.strip()
    now = now or datetime.now().astimezone()
    m = re.fullmatch(r"\+(\d+)\s*([hm])", s, re.IGNORECASE)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        dt = now + (timedelta(hours=n) if unit == "h" else timedelta(minutes=n))
        return int(dt.timestamp() * 1000)
    tod = parse_time_of_day(s)
    if tod is not None:
        dt = now.replace(hour=tod[0], minute=tod[1], second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return int(dt.timestamp() * 1000)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ T](.+)", s)
    tod = parse_time_of_day(m.group(4)) if m else None
    if not m or tod is None:
        raise ValueError(f"couldn't parse time {s!r}: use 22:00 or 10pm, +3h, +90m, or '2026-09-15 22:00'")
    dt = now.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)),
                     hour=tod[0], minute=tod[1], second=0, microsecond=0)
    if dt <= now:
        raise ValueError(f"{dt:%Y-%m-%d %H:%M} is in the past")
    return int(dt.timestamp() * 1000)


def fmt_when(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M")


def cmd_send(a, settings: Settings) -> None:
    if not sessions.session_alive(a.id):
        print(f"note: no running session '{a.id}': it will only deliver once that session runs", file=sys.stderr)
    t = _transport(settings, a.id)
    try:
        t.write_command(a.text)
        t.flush()
    finally:
        t.stop()
    print(f"Sent to {a.id}.")


def cmd_schedule(a, settings: Settings) -> None:
    if not sessions.session_alive(a.id):
        print(f"note: no running session '{a.id}': a scheduled prompt only delivers to a running session",
              file=sys.stderr)
    try:
        ms = parse_when(a.at)
    except ValueError as e:
        die(str(e))
    t = _transport(settings, a.id)
    try:
        t.write_command(a.text, deliver_at=ms)
        t.flush()
    finally:
        t.stop()
    print(f"Scheduled for {fmt_when(ms)} (this computer's time): {a.text}")


def cmd_scheduled(a, settings: Settings) -> None:
    t = _transport(settings, a.id)
    try:
        # The inbox fills from the relay's replay right after the hello; wait for it.
        items = []
        for _ in range(20):
            items = t.list_scheduled()
            if items:
                break
            time.sleep(0.1)
        n = a.delete or a.cancel or a.edit
        if a.by_id:
            # Address by command id (a GUI never trusts list positions).
            item = next((it for it in items if it["id"] == a.by_id), None)
            if item is None:
                die(f"no scheduled prompt {a.by_id} for {a.id}")
            n = items.index(item) + 1
            if a.edit is None and a.delete is None and a.cancel is None:
                a.delete = n
        if n is not None or a.by_id:
            if a.by_id is None and (n < 1 or n > len(items)):
                die(f"no scheduled prompt #{n} for {a.id} (have {len(items)})")
            item = items[n - 1] if a.by_id is None else item
            if a.edit is not None:
                new_text = a.text or item["text"]
                try:
                    new_ms = parse_when(a.at) if a.at else item["deliver_at"]
                except ValueError as e:
                    die(str(e))
                t.update_command(item["id"], new_text, new_ms)
                print(f"Updated #{n}: {fmt_when(new_ms)}  {new_text}")
            else:
                t.cancel_command(item["id"])
                print(f"Deleted #{n}.")
            t.flush()
            return
        if a.json:
            print(json.dumps(items, sort_keys=True))
            return
        if not items:
            print(f"No scheduled prompts for {a.id}.")
            return
        for i, it in enumerate(items, 1):
            preview = (it["text"][:60] + "...") if len(it["text"]) > 60 else it["text"]
            print(f"{i}. {fmt_when(it['deliver_at'])}  {preview}")
    finally:
        t.stop()


# ── host state ───────────────────────────────────────────────────────────────


def cmd_mobile_mode(a, settings: Settings) -> None:
    if a.state == "on":
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.mobile_mode_file.write_text("manual")
    elif a.state == "off":
        settings.mobile_mode_file.unlink(missing_ok=True)
    print("mobile mode:", "on" if settings.mobile_mode_file.exists() else "off")


def cmd_answer(a, settings: Settings) -> None:
    """Answer the question a session is waiting on, from this computer."""
    from aaw_core.transport.base import LocalLog
    t = _transport(settings, a.id)
    try:
        question_id = a.question or (t.get_project().get("pending_question_id") or "")
        if not question_id:
            die(f"{a.id} is not waiting for an answer")
        asked = next((e for e in reversed(LocalLog(settings.sessions_dir, a.id).read())
                      if e.get("type") == "question" and e.get("id") == question_id), {})
        options = [str(o) for o in asked.get("options") or []]
        answer = " ".join(a.answer).strip()
        if not answer:
            print(asked.get("question") or "(the question is not in this computer's log)")
            if asked.get("context"):
                print(textwrap.indent(str(asked["context"]), "  "))
            die("give an answer" + (": " + " | ".join(options) if options else ""))
        if options:
            match = [o for o in options if o.lower() == answer.lower()]
            if not match:
                die(f"the answer must be one of: {' | '.join(options)}")
            answer = match[0]
        t.write_answer(question_id, answer)
        t.flush()
    finally:
        t.stop()
    print(f"Answered {a.id}: {answer}")


# ── agents that know about each other (see host/peers.py) ───────────────────


def _caller() -> tuple[str, str]:
    """The session this command runs in (its id and agent), or ("", "") in a plain terminal."""
    return os.environ.get("AAW_PROJECT", ""), os.environ.get("AAW_AGENT", "")


def _text_arg(words: list[str] | None) -> str:
    """The text given as arguments, or read from stdin when there is none."""
    text = " ".join(words or []).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    return text


def cmd_peers(a, settings: Settings) -> None:
    from aaw_core.host import peers
    if a.switch in ("on", "off"):
        save_config(settings.state_dir, peers=a.switch == "on")
        print("Agents may now propose notes and messages to other sessions; you approve each one on a card."
              if a.switch == "on" else "Agents can no longer propose notes or messages to other sessions.")
        return
    me, _ = _caller()
    rows = peers.others(settings, me)
    if a.json:
        print(json.dumps({"enabled": settings.peers, "sessions": rows}))
        return
    if not rows:
        print("No sessions are running.")
    for r in rows:
        mark = "  (this session)" if r["me"] else ""
        print(f"  {r['id']:24s} {r['agent'] or '?':8s} {r['status']:8s} {r['path']}{mark}")
    if not settings.peers:
        print(peers.OFF_HINT)
    elif me:
        print('Send one a message with: aaw tell <session> "<text>"')


def cmd_tell(a, settings: Settings) -> None:
    from aaw_core.host import peers
    me, agent = _caller()
    if not me:
        die("`aaw tell` is for an agent inside a session. From a terminal, use `aaw send <session> <text>`.")
    if not settings.peers:
        die(peers.OFF_HINT)
    text = _text_arg(a.text)
    if not text:
        die('nothing to send. Usage: aaw tell <session> "<text>"')
    if len(text) > peers.MAX_MESSAGE:
        die(f"the message is longer than {peers.MAX_MESSAGE} characters. Write it to a file and send its path.")
    if a.id == me:
        die("that is this session")
    running = peers.others(settings, me)
    if not any(r["id"] == a.id for r in running):
        names = ", ".join(r["id"] for r in running if not r["me"]) or "none"
        die(f"{a.id} is not running. Running sessions: {names}")
    if not settings.relay_url or load_identity(settings) is None:
        die("this computer is not linked to a phone, so nobody can approve the message")
    if peers.pending_request(settings, me):
        die("one proposal from this session is already waiting for the user. Wait for its outcome.")
    to_agent = next((r["agent"] for r in running if r["id"] == a.id), "")
    peers.submit(settings, {"kind": "tell", "from": me, "agent": agent, "to": a.id,
                            "to_agent": to_agent, "text": text})
    print(f"Asked the user to approve your message to {a.id}. It is sent only if they approve.\n"
          "End your turn now: the answer, or word that it was not sent, arrives as a new prompt.")


def cmd_know(a, settings: Settings) -> None:
    from aaw_core.host import knowledge, peers
    me, agent = _caller()
    action = a.action or "list"
    if action == "list":
        notes = knowledge.list_notes(settings)
        if a.json:
            print(json.dumps([{"name": n.name, "title": n.title, "description": n.description} for n in notes]))
            return
        if not notes:
            print(f"No shared notes yet. They live in {knowledge.folder(settings)}")
        for n in notes:
            print(f"  {n.name:24s} {n.title}" + (f": {n.description}" if n.description else ""))
        return
    if action == "search":
        if not a.args:
            die("usage: aaw know search <words>")
        found = knowledge.search(settings, a.args)
        if not found:
            print("No note matches.")
        for n in found:
            print(f"## {n.name}: {n.title}\n{n.body}\n")
        return
    name = (a.args or [""])[0]
    if action == "show":
        note = knowledge.read(settings, name)
        if note is None:
            die(f"no note named {name!r}. `aaw know` lists them.")
        print(f"# {note.title}\n" + (f"{note.description}\n" if note.description else "")
              + (f"(added {note.added})\n" if note.added else "") + f"\n{note.body}")
        return
    if action == "remove":
        if knowledge.read(settings, name) is None:
            die(f"no note named {name!r}")
        if not me:  # the user, in a terminal: their own file
            knowledge.remove(settings, name)
            print(f"Removed {name}.")
            return
        _propose(settings, peers, me, {"kind": "know_remove", "from": me, "agent": agent, "name": name})
        print(f"Asked the user to approve removing {name}.")
        return
    # add
    body = (a.text or "").strip() or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    title = (a.title or name).strip()
    why_not = knowledge.check(name, title, body)
    if why_not:
        die(why_not)
    note = knowledge.Note(name=name, title=title, description=(a.description or "").strip(),
                          added=knowledge.stamp(agent, me), body=body)
    if not me:
        print(f"Saved {knowledge.write(settings, note)}")
        return
    from dataclasses import asdict
    _propose(settings, peers, me, {"kind": "know_add", "from": me, "agent": agent, "note": asdict(note)})
    print(f"Asked the user to approve the note {name}. It is saved only if they approve; carry on meanwhile.")


def _propose(settings: Settings, peers, me: str, request: dict) -> None:
    if not settings.peers:
        die(peers.OFF_HINT)
    if not settings.relay_url or load_identity(settings) is None:
        die("this computer is not linked to a phone, so nobody can approve this")
    if peers.pending_request(settings, me):
        die("one proposal from this session is already waiting for the user. Wait for its outcome.")
    peers.submit(settings, request)


def cmd_models(a, settings: Settings) -> None:
    scoot = sessions.scoot_status()
    models = scoot[0] if scoot is not None else None
    if a.json:
        print(json.dumps(models if models is not None else None))
        return
    if models is None:
        die("scoot is not installed (pipx install scootcli)")
    if not models:
        print("No models available. Add a key with `scoot auth set <provider>`, or start Ollama.")
        for provider, why in sorted(scoot[1].items()):
            print(f"  {provider}: {why}")
        return
    for m in models:
        if not a.provider or m.startswith(a.provider + "/"):
            print(m)


OLLAMA_START_SAYS = {
    "running": "Ollama is already running.",
    "started": "Ollama was not running; started it.",
    "not_installed": "Ollama is not installed on this computer. Get it from https://ollama.com",
    "failed": "Ollama is installed but did not start. Start it by hand.",
}


def cmd_start_ollama(a, settings: Settings) -> None:
    from aaw_core.host import ollama
    result = ollama.start(settings.state_dir / "ollama.log")
    if a.json:
        print(json.dumps({"result": result}))
    elif result in ("running", "started"):
        print(OLLAMA_START_SAYS[result])
    else:
        die(OLLAMA_START_SAYS[result])


def cmd_install_hooks(a, settings: Settings) -> None:
    hooks_installer.install_all()


def cmd_shell_integration(a, settings: Settings) -> None:
    rc = hooks_installer.shell_rc()
    if a.state == "on":
        added = hooks_installer.add_shell_integration()
        print(f"{'added to' if added else 'already in'} {rc}: typing claude, codex, gemini, grok, cursor-agent "
              "or scoot in a folder starts a bridged session.")
        if added:
            print(f"Takes effect in new terminals, or now with: source {rc}")
    elif a.state == "off":
        removed = hooks_installer.remove_shell_integration()
        print(f"{'removed from' if removed else 'not in'} {rc}.")
    else:
        print(f"shell integration: {'on' if hooks_installer.shell_integration_status() else 'off'} ({rc})")


def cmd_shell_init(a, settings: Settings) -> None:
    """The shell functions, for `eval "$(aaw shell-init)"` in a shell rc."""
    sys.stdout.write((SCRIPTS_DIR / "shell_integration.sh").read_text())


def cmd_service(a, settings: Settings) -> None:
    """The supervisor as a login service: systemd --user on Linux, launchd on macOS."""
    if a.action == "install":
        lines = service.install(settings)
        print("\n".join(lines))
        if any("failed" in ln for ln in lines):
            sys.exit(1)  # a GUI driving this must see the failure
        if sys.platform == "darwin" and settings.local_notifications:
            print("Desktop banners: on" + FOCUS_HINT)
    elif a.action == "uninstall":
        print("\n".join(service.uninstall()))
    elif a.action == "start":
        print(service.start())
    elif a.action == "stop":
        print(service.stop())
    else:
        print(f"supervisor service: {service.status()} ({service.unit_path()})")


def cmd_quit(a, settings: Settings) -> None:
    """Turn the host off: stop the supervisor service, then every session and daemon. It
    stays installed and linked; `aaw service start` (or the next login) brings it back."""
    print("Stopping Agents At Work Core...")
    print(service.stop())
    n = _stop_all(settings)
    print(f"  stopped {n} session{'s' if n != 1 else ''}" if n else "  no sessions were running")
    settings.enabled_flag.unlink(missing_ok=True)
    print("Stopped.")


def cmd_uninstall(a, settings: Settings) -> None:
    """Remove the host from this machine: stop everything, remove the service unit, strip
    the hooks from every agent, drop the shell integration, delete the state dir."""
    if not a.yes:
        try:
            resp = input("Remove Agents At Work Core from this machine (stop it, remove hooks, service, "
                         "local state)? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return
    print("Uninstalling...")
    print("\n".join(service.uninstall()))
    n = _stop_all(settings)
    print(f"  stopped {n} session{'s' if n != 1 else ''}" if n else "  no sessions were running")
    for p in hooks_installer.remove_all():
        print(f"  removed hooks from {p}")
    if hooks_installer.remove_shell_integration():
        print(f"  removed the shell integration from {hooks_installer.shell_rc()}")
    if settings.state_dir.exists():
        shutil.rmtree(settings.state_dir, ignore_errors=True)
        print(f"  removed {settings.state_dir}")
    print("Done. To remove the package itself: pipx uninstall aaw-core (or pip uninstall aaw-core).")


def cmd_supervisor(a, settings: Settings) -> None:
    from aaw_core.host.supervisor import Supervisor
    Supervisor(settings).run()


# ── entry ────────────────────────────────────────────────────────────────────


ROOT_REFUSAL = """\
aaw does not run as root.
Under root it would keep its key, state and sessions in root's home, and it could not
manage its login service (systemctl --user), so sessions would not reach your phone.

Create or switch to a normal user and install there:
  adduser you            # once, if the machine only has root
  su - you
  curl -fsSL https://agentsatwork.app/install-linux.sh | bash

An install made as root earlier can be removed here with: aaw uninstall
"""
# What root may still run: looking around, and cleaning up an install made as root.
_ROOT_MAY_RUN = frozenset({"uninstall", "quit", "-h", "--help", "--version"})


def _refused_as_root(argv: list[str]) -> bool:
    """Under root (or sudo) $HOME is root's: the session key, the state and the folder
    browser all resolve there, and `systemctl --user` cannot manage the service. A warning
    was not enough: people ran on and reported sessions that never reached the phone."""
    if getattr(os, "geteuid", lambda: 1)() != 0:
        return False
    return not (argv and argv[0] in _ROOT_MAY_RUN)


def build_parser() -> argparse.ArgumentParser:
    agents = ", ".join(sessions.AGENTS)
    p = argparse.ArgumentParser(
        prog="aaw",
        description="Agents At Work Core: run Claude Code, Codex, Gemini, Grok, Cursor or scoot on this "
                    "machine and follow them from your phone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              aaw link                               show the QR code that pairs a phone
              aaw service install                    run the supervisor at every login (systemd --user / launchd)
              aaw start ~/proj                      start the only installed agent (Claude when several)
              aaw start ~/proj --agent codex         a second agent on the same folder runs alongside as proj-codex
              aaw start ~/proj --agent scoot --model ollama/qwen2.5:latest
              aaw status                             what is running
              aaw stop proj-codex                    stop one session, the others keep running
              aaw feed proj                          the feed, paged so it scrolls and `/` searches
              aaw schedule proj --at 10pm "run the deploy"   queue a prompt (22:00, +3h, '2026-09-15 22:00')
              aaw scheduled proj                     list scheduled prompts (--delete N / --edit N --at ...)
              aaw mobile-mode on                     route permission prompts to the phone

            settings: environment variables or ~/.aaw/config.json
              AAW_RELAY_URL / relay_url              wss://... the relay this computer and the phone meet at
              AAW_COMPUTER_NAME / computer_name      the name shown on the phone (default: the Computer Name)
              AAW_BROWSE_ROOTS / browse_roots        folders the phone may browse and start sessions in (default ~)
              AAW_KEEP_AWAKE / keep_awake            keep the computer awake while a session runs (default true)
              AAW_LOCAL_NOTIFICATIONS                desktop banners (default true)
              AAW_WAITING_ALERT_SECONDS              desktop alert when a phone question waits this long (0 = off)
            """))
    p.add_argument("--version", action="version", version=f"aaw-core {__version__}")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("link", help="show the QR code to pair a phone (asks which relay to use the first time)")
    s.add_argument("--relay", metavar="URL", help="use this relay (saved to config.json); default: ask once")
    s.add_argument("--show-payload", action="store_true")
    s.add_argument("--json", action="store_true",
                   help="print the pairing payload only, no QR (for a GUI; picks the hosted relay when none is set)")
    s.add_argument("--light", action="store_true", help="for a light terminal background")
    s.add_argument("--force", action="store_true", help="print the QR even when the terminal is too narrow")
    s.set_defaults(fn=cmd_link)

    s = sp.add_parser("start", help="start an agent session on a folder (--agent picks which)",
                      description="Start an agent on a folder, then attach to its tmux session "
                                  "(detach with Ctrl-b d; the session keeps running).")
    s.add_argument("dir", help="project folder")
    s.add_argument("--agent", default=None, choices=list(sessions.AGENTS), metavar="AGENT",
                   help=f"one of: {agents} (default: the only installed agent, else claude)")
    s.add_argument("--model", default=None, help="model for --agent scoot (provider/model); see `aaw models`")
    s.add_argument("--id", help="session id to use instead of the folder name")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask before opening a second agent on a folder")
    s.add_argument("--no-attach", action="store_true", help="start without attaching to the tmux session")
    s.add_argument("--porcelain", action="store_true", help=argparse.SUPPRESS)  # shell integration: id only
    s.set_defaults(fn=cmd_start)

    s = sp.add_parser("stop", help="stop one session, or all of them with no id / --all")
    s.add_argument("id", nargs="?")
    s.add_argument("--all", action="store_true", help="stop every session")
    s.set_defaults(fn=cmd_stop)
    s = sp.add_parser("status", help="sessions, daemons, supervisor, pairing")
    s.add_argument("--json", action="store_true", help="machine-readable, with the mirrored project documents")
    s.add_argument("--agents", action="store_true", help="with --json: also probe the installed agents and models")
    s.set_defaults(fn=cmd_status)

    s = sp.add_parser("feed", help="show a session's conversation (paged; -f to follow, --remote for the relay's copy)")
    s.add_argument("id")
    s.add_argument("--follow", "-f", action="store_true", help="stream the live local feed (no pager)")
    s.add_argument("--remote", action="store_true", help="the retained conversation from the relay, decrypted")
    s.add_argument("--all", action="store_true", help="with --remote, fetch everything retained")
    s.add_argument("--limit", type=int, default=2000, help="with --remote, max events to fetch (default 2000)")
    s.set_defaults(fn=cmd_feed)

    s = sp.add_parser("send", help="send a prompt to a running session now")
    s.add_argument("id")
    s.add_argument("text")
    s.set_defaults(fn=cmd_send)
    s = sp.add_parser("schedule", help="queue a prompt to send later (e.g. when the token limit resets)")
    s.add_argument("id")
    s.add_argument("text", help="the prompt to send")
    s.add_argument("--at", required=True, metavar="WHEN",
                   help="time of day 24h '22:00' or 12h '10pm' / '10:30 PM' (next occurrence); "
                        "or +3h / +90m; or '2026-09-15 22:00'. This computer's clock.")
    s.set_defaults(fn=cmd_schedule)
    s = sp.add_parser("scheduled", help="list, edit, or delete a session's scheduled prompts")
    s.add_argument("id")
    s.add_argument("text", nargs="?", help="new text when editing")
    s.add_argument("--edit", type=int, metavar="N")
    s.add_argument("--delete", type=int, metavar="N")
    s.add_argument("--cancel", type=int, metavar="N", help="alias of --delete")
    s.add_argument("--at", metavar="WHEN", help="new time when editing")
    s.add_argument("--json", action="store_true", help="the list as JSON")
    s.add_argument("--id", dest="by_id", metavar="COMMAND_ID",
                   help="address one prompt by its id instead of its number (with --edit N or alone to delete)")
    s.set_defaults(fn=cmd_scheduled)

    s = sp.add_parser("mobile-mode", help="route permission prompts to the phone")
    s.add_argument("state", choices=["on", "off", "status"])
    s.set_defaults(fn=cmd_mobile_mode)
    s = sp.add_parser("answer", help="answer the question a session is waiting on (as the phone would)")
    s.add_argument("id")
    s.add_argument("answer", nargs="*", help="one of the question's options, or free text when it has none")
    s.add_argument("--question", help="the question's id (default: the one the session waits on)")
    s.set_defaults(fn=cmd_answer)
    s = sp.add_parser("peers", help="the other agent sessions on this computer; `on`/`off` lets agents reach them")
    s.add_argument("switch", nargs="?", choices=["on", "off"],
                   help="let agents propose notes and messages (each one needs your approval), or stop that")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_peers)
    s = sp.add_parser("tell", help="(for an agent) propose a message to another session; the user approves it")
    s.add_argument("id", help="the session to message, as `aaw peers` lists it")
    s.add_argument("text", nargs="*", help="the message (or pass it on stdin)")
    s.set_defaults(fn=cmd_tell)
    s = sp.add_parser("know", help="notes shared by every agent session: list, show, search, add, remove")
    s.add_argument("action", nargs="?", choices=["list", "show", "search", "add", "remove"])
    s.add_argument("args", nargs="*", help="a note's name, or the words to search for")
    s.add_argument("--title")
    s.add_argument("--description", help="one line saying what the note is for")
    s.add_argument("--text", help="the note itself (or pass it on stdin)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_know)
    s = sp.add_parser("models", help="list the models scoot can run")
    s.add_argument("--provider", help="only this provider (openai, anthropic, ollama)")
    s.add_argument("--json", action="store_true", help="the list as JSON (null when scoot is not installed)")
    s.set_defaults(fn=cmd_models)
    s = sp.add_parser("start-ollama", help="start Ollama when it is installed and not running (for scoot's local models)")
    s.add_argument("--json", action="store_true", help='print {"result": running|started|not_installed|failed}')
    s.set_defaults(fn=cmd_start_ollama)
    sp.add_parser("install-hooks", help="register the hooks with every installed agent").set_defaults(
        fn=cmd_install_hooks)
    s = sp.add_parser("shell-integration", help="make a plain `claude` (codex, ...) start a bridged session")
    s.add_argument("state", choices=["on", "off", "status"])
    s.set_defaults(fn=cmd_shell_integration)
    sp.add_parser("shell-init", help="print the shell functions (for eval in a shell rc)").set_defaults(
        fn=cmd_shell_init)
    sp.add_parser("supervisor", help="run the supervisor in the foreground").set_defaults(fn=cmd_supervisor)
    s = sp.add_parser("service", help="the supervisor as a login service (systemd --user / launchd)")
    s.add_argument("action", choices=["install", "uninstall", "start", "stop", "status"])
    s.set_defaults(fn=cmd_service)
    sp.add_parser("quit", help="turn the host off (stop the supervisor and every session); stays installed"
                  ).set_defaults(fn=cmd_quit)
    s = sp.add_parser("uninstall", help="remove the host from this machine (hooks, service, local state)")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask to confirm")
    s.set_defaults(fn=cmd_uninstall)
    return p


def main(argv: list[str] | None = None) -> int:
    if _refused_as_root(sys.argv[1:] if argv is None else argv):
        sys.stderr.write(ROOT_REFUSAL)
        return 1
    hooks_installer.extend_path()  # so `aaw start` finds the agent from any shell
    a = build_parser().parse_args(argv)
    try:
        a.fn(a, load_settings())
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as e:
        # A pip-only install on a machine without tmux: every session lives in tmux.
        if os.path.basename(str(e.filename or "")) != "tmux":
            raise
        print(TMUX_MISSING, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
