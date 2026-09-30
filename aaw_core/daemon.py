"""The session daemon: one per bridged agent session.

It keeps the relay socket, watches the agent's tmux pane for prompts and idle
states, forwards questions to the phone and types the answers back, and executes
commands from the phone (text, /restart, /stop, /status, /usage), including
scheduled prompts once their time comes.

    python -m aaw_core.daemon --project-dir PATH [--project-id ID] [--agent NAME]

The project id is the session identity (the feed's project id, the tmux name minus
"aaw-", the log file stem). It defaults to the folder basename; a parallel session on
the same folder is just a second id ("<base>-codex") passed explicitly. Nothing here
derives an id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from aaw_core import __version__
from aaw_core.config import Settings, load_settings
from aaw_core.hooks.common import (
    is_mobile_mode,
    read_key,
    set_mobile_mode,
    tmux_session,
)
from aaw_core.hooks.on_stop_cursor import response_from_transcript as cursor_response_from_transcript
from aaw_core.hooks.on_subagent import update_subagents
from aaw_core.host import detect, tmux, usage
from aaw_core.host.identity import load_identity
from aaw_core.transport.relay import RelayTransport

UNDELIVERED_NOTICE = ("Your message did not reach the session (tmux was unresponsive). "
                      "Please send it again.")
COMMAND_POLL_S = 2.0
HEARTBEAT_S = 30.0
LOOP_SLEEP_S = 0.5  # short, so a prompt reaches the phone before the user answers it on the desktop


def log(msg: str, *, err: bool = False) -> None:
    print(f"[daemon] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def build_transport(settings: Settings, project_id: str) -> RelayTransport | None:
    if not settings.relay_url:
        log("no relay configured (set AAW_RELAY_URL); cannot bridge this session", err=True)
        return None
    ident = load_identity(settings)
    if ident is None:
        log("this host is not set up yet; run `aaw link` first", err=True)
        return None
    return RelayTransport(relay_url=settings.relay_url, token=ident.token, computer_id=ident.computer_id,
                          project_id=project_id, sessions_dir=settings.sessions_dir,
                          computer_name=ident.computer_name, enc_key=read_key(settings)).start()


def project_doc(transport: RelayTransport) -> dict:
    """The project document, or {} when the relay cannot be reached in time."""
    try:
        return transport.get_project()
    except Exception as e:  # noqa: BLE001 - a failed read must never stop the loop
        log(f"project read failed: {e}", err=True)
        return {}


# ── commands from the phone ─────────────────────────────────────────────────


def forward_text(transport: RelayTransport, session: str, agent: str, text: str) -> None:
    """Type a message into the agent and echo it to the feed once it was actually
    delivered; otherwise say so, so the user resends instead of waiting."""
    if tmux.send_text(text, session, agent):
        transport.write_event("message", {"role": "user", "content": text, "agent": agent}, via="daemon")
        return
    log(f"message NOT delivered after {tmux.SEND_ATTEMPTS} attempts: {text[:60]!r}", err=True)
    try:
        transport.write_notification(UNDELIVERED_NOTICE, level="error")
    except Exception as e:  # noqa: BLE001
        log(f"could not post the undelivered notice: {e}", err=True)


def flush_pending_message(transport: RelayTransport, session: str, agent: str) -> None:
    """Send a message queued while a dialog was open (opportunistic takeover). Called
    after every answer path; a failed send leaves it queued for the next flush point."""
    try:
        queued = project_doc(transport).get("pending_message", "")
        if not queued:
            return
        time.sleep(0.3)  # let the prompt clear from the pane
        if not tmux.send_text(queued, session, agent):
            log(f"pending message not delivered, keeping it queued: {queued[:50]}", err=True)
            return
        transport.write_event("message", {"role": "user", "content": queued, "agent": agent}, via="daemon")
        transport.update_project(pending_message="")
        log(f"flushed the pending message: {queued[:50]}")
    except Exception as e:  # noqa: BLE001
        log(f"pending message flush failed: {e}", err=True)


def handle_command(cmd: dict, transport: RelayTransport, settings: Settings, *, session: str, agent: str,
                   project_dir: Path, project_id: str) -> None:
    """Route one command document: plain text is typed into the agent; /restart, /stop,
    /status and /usage act on the session. Legacy command types are still honored."""
    payload = cmd.get("payload") or {}
    command = payload.get("command", "")
    args = (payload.get("args") or "").strip()

    # Any phone interaction renews mobile mode so the pre-tool hook asks the phone.
    # A command from the computer's own tools is marked source=desktop and does not.
    if payload.get("source") != "desktop":
        set_mobile_mode(settings)

    # Opportunistic takeover: a message sent while a dialog owns the keyboard would be
    # eaten by it, so queue it; the answer paths flush it once the dialog is gone.
    if command == "text" and args and not args.startswith("/"):
        try:
            if detect.dialog_is_open(tmux.visible_pane(session), agent):
                transport.update_project(pending_message=args)
                log("a prompt is open; message queued until it is answered")
                return
        except Exception as e:  # noqa: BLE001
            log(f"takeover check failed: {e}", err=True)

    def restart() -> None:
        # Decided before the restart: the new session resumes the conversation when the
        # folder already has history, and then the phone's feed must keep matching it.
        resumed = tmux.resumes_conversation(agent, project_dir)
        tmux.restart_agent(session, project_dir, agent, project_id)
        if resumed:
            log("/restart: the agent resumes its conversation; the feed is kept")
        else:
            # A fresh conversation: reset the feed to match.
            transport.clear_events()
            transport.init_local_log(is_reconnect=False)
        update_subagents(settings, project_id, clear=True)
        transport.set_project_status("running", pending_question_id="")
        transport.update_project(background_agents=[])

    def stop() -> None:
        tmux.tmux_run(["kill-session", "-t", session], capture_output=True)
        try:
            update_subagents(settings, project_id, clear=True)
            transport.set_project_status("stopped", pending_question_id="")
            transport.update_project(background_agents=[])
            transport.delete_local_log()
        except Exception as e:  # noqa: BLE001
            log(f"could not write stopped status on /stop: {e}", err=True)

    def status() -> None:
        running = tmux.has_session(session)
        transport.write_event("message", {"role": "assistant",
                                          "content": f"Session '{session}' is {'running' if running else 'not running'}."})

    if command == "text":
        if args.startswith("/restart"):
            restart()
        elif args == "/stop":
            stop()
        elif args == "/status":
            status()
        elif args == "/usage":
            usage.fetch_usage(transport, project_dir, agent)
        else:
            transport.set_project_status("running")
            forward_text(transport, session, agent, args)
    elif command in ("btw", "prompt"):  # legacy
        transport.set_project_status("running")
        forward_text(transport, session, agent, args)
    elif command == "restart":
        restart()
    elif command == "stop":
        stop()
    elif command == "status":
        status()


# ── Claude Code diagnostics ─────────────────────────────────────────────────


def check_claude_ui_version(settings: Settings, prompt: dict) -> None:
    """Record the Claude Code version + prompt structure; warn when either changes,
    since that is when the pane detectors need re-verification."""
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5, check=False)
        version = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    fingerprint = f"{version}|{prompt['structure']}"
    path = settings.state_dir / "claude_ui_fingerprint"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stored = path.read_text().strip()
            if stored and stored != fingerprint:
                log(f"Claude Code version/UI changed: stored {stored!r}, now {fingerprint!r}; "
                    "permission prompt detection may need re-calibration")
        path.write_text(fingerprint)
    except OSError:
        pass


def resolve_claude_pid(session: str) -> int | None:
    """The `claude` process inside the session, for its session-state file. Never raises."""
    try:
        pane_pid = tmux.tmux_run(["list-panes", "-t", session, "-F", "#{pane_pid}"],
                                 capture_output=True, text=True, timeout=5).stdout.strip().split("\n")[0]
        if not pane_pid:
            return None
        out = subprocess.run(["pgrep", "-P", pane_pid, "-f", "claude"], capture_output=True, text=True,
                             timeout=5, check=False).stdout.strip()
        for pid in out.split("\n"):
            if pid.strip():
                return int(pid.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def claude_session_status(pid: int) -> dict | None:
    """Claude Code's own live session-state file (an undocumented internal): a diagnostic
    cross-check only, never the source of truth."""
    try:
        return json.loads((Path.home() / ".claude" / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None


# ── the session loop ────────────────────────────────────────────────────────


def run_session(settings: Settings, project_dir: Path, agent: str = "claude", project_id: str | None = None) -> int:
    if not settings.enabled_flag.exists():
        return 0  # the host is off; skip silently
    project_id = project_id or project_dir.name
    session = tmux_session(project_id)
    transport = build_transport(settings, project_id)
    if transport is None:
        return 1
    if not transport.wait_connected(15):
        log("relay not reachable yet; continuing, the transport reconnects on its own")

    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Init. Only clear the feed for a new session: if the tmux session already exists this
    # daemon is reconnecting (orphan recovery, host restart) and the history must survive.
    # A changed agent on the same folder counts as a new session.
    is_reconnect = tmux.has_session(session)
    if is_reconnect:
        stored_agent = project_doc(transport).get("agent", agent)
        if stored_agent != agent:
            is_reconnect = False
            log(f"agent changed {stored_agent!r} to {agent!r}; treating as a new session")
    log(f"session {project_id} ({agent}), reconnect={is_reconnect}, aaw-core {__version__}")
    try:
        if not is_reconnect:
            transport.clear_events()
            update_subagents(settings, project_id, clear=True)
            transport.update_project(background_agents=[])
        transport.init_local_log(is_reconnect)
        transport.set_project_status("running", pending_question_id="" if not is_reconnect else None)
        transport.update_computer(status="running", daemon_version=__version__)
        transport.update_project(project_path=transport.encrypt_field(str(project_dir)), agent=agent)
    except Exception as e:  # noqa: BLE001
        log(f"init failed: {e}", err=True)

    now = time.time()
    last_heartbeat = 0.0
    last_cmd_poll = 0.0
    backoff_until = 0.0

    # Claude permission prompt state
    perm_question_id: str | None = None
    perm_hash: str | None = None
    perm_options: list = []
    # Claude's "trust this folder" dialog (first open of a folder)
    trust_question_id: str | None = None
    trust_hash: str | None = None
    perm_first_seen = 0.0
    # Cursor native prompt state
    cn_question_id: str | None = None
    cn_hash: str | None = None
    cn_session_key = "Tab"
    cn_first_seen = 0.0
    cursor_error_hash: str | None = None
    # AskUserQuestion state (single-select), and multi-select
    aq_question_id: str | None = None
    aq_hash: str | None = None
    aq_options: list = []
    ms_hash: str | None = None
    # idle / capture state
    claude_retry_hash: str | None = None
    claude_pid: int | None = None
    last_mismatch_warn = 0.0
    stopped_written = False
    tmux_gone = 0
    idle_since: float | None = None
    idle_written = False
    gemini_idle_since: float | None = None
    gemini_idle_written = False
    gemini_last_hash: str | None = None
    cursor_idle_since: float | None = None
    cursor_idle_written = False
    cursor_resp_check_at = 0.0

    def auto_approve() -> bool:
        return bool(project_doc(transport).get("auto_approve"))

    def send_question(question: str, options: list, kind: str, context: str, **extra) -> str:
        event_id = transport.write_event("question", {"question": question, "options": options, "kind": kind,
                                                      "timeout_at": int(time.time()) + 300, "context": context,
                                                      "agent": agent, **extra})
        transport.set_project_status("waiting", pending_question_id=event_id)
        return event_id

    while running:
        now = time.time()

        if now - last_heartbeat >= HEARTBEAT_S:
            if not settings.enabled_flag.exists():
                log("host disabled; exiting")
                break
            try:
                transport.update_computer()
                last_heartbeat = now
            except Exception as e:  # noqa: BLE001
                log(f"heartbeat failed: {e}", err=True)

        try:
            alive = tmux.has_session(session)
            if not alive:
                tmux_gone += 1
                # has-session fails transiently; require a streak, then positive confirmation.
                if tmux_gone >= 6:
                    listed = tmux.tmux_run(["list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
                    no_server = listed.returncode != 0 and ("no server running" in (listed.stderr or "")
                                                             or "error connecting to" in (listed.stderr or ""))
                    gone = no_server or (listed.returncode == 0 and session not in listed.stdout.splitlines())
                    if gone:
                        if not stopped_written:
                            try:
                                transport.set_project_status("stopped", pending_question_id="")
                                transport.delete_local_log()
                                stopped_written = True
                                log("tmux session gone (confirmed); wrote stopped, exiting")
                            except Exception as e:  # noqa: BLE001
                                log(f"could not write stopped on tmux exit: {e}", err=True)
                        running = False
                    else:
                        log(f"has-session failed {tmux_gone}x but the session is still listed; not exiting")
                        tmux_gone = 0
                else:
                    log(f"tmux check failed ({tmux_gone}/6); will retry")
            else:
                tmux_gone = 0

            if alive:
                pane = tmux.visible_pane(session)

                # ── Claude Code "trust this folder" dialog ─────────────────
                trust = detect.detect_claude_trust_dialog(pane) if agent == "claude" else None
                if trust and trust["hash"] != trust_hash:
                    trust_hash = trust["hash"]
                    if is_mobile_mode(settings):
                        trust_question_id = send_question(trust["question"], trust["options"], "permission",
                                                          "Claude Code trust dialog")
                        log(f"trust dialog sent to the phone ({trust_question_id[:8]})")
                    else:
                        trust_question_id = None
                        log("trust dialog detected (desktop mode; not forwarding)")
                elif not trust and trust_hash:
                    if trust_question_id:
                        transport.set_project_status("running", pending_question_id="")
                        log("trust dialog gone (answered on the desktop)")
                    trust_question_id, trust_hash = None, None
                    flush_pending_message(transport, session, agent)

                # ── Claude Code permission prompt ──────────────────────────
                perm = detect.detect_permission_prompt(pane)
                if perm and perm["hash"] != perm_hash:
                    check_claude_ui_version(settings, perm)
                    perm_hash, perm_first_seen, perm_options = perm["hash"], time.monotonic(), perm["options"]
                    if auto_approve() and detect.is_yes_no_shaped(perm["options"]):
                        tmux.send_keys(session, "1")  # first option: Yes
                        perm_question_id = None
                        log("permission prompt auto-approved (key 1)")
                        flush_pending_message(transport, session, agent)
                    elif is_mobile_mode(settings):
                        perm_question_id = send_question(perm["question"], perm["options"], "permission",
                                                         "Claude Code permission prompt")
                        log(f"permission prompt sent to the phone ({perm_question_id[:8]})")
                    else:
                        perm_question_id = None
                        log("permission prompt detected (desktop mode; not forwarding)")
                elif perm and perm["hash"] == perm_hash:
                    if time.monotonic() - perm_first_seen >= 5.0 and auto_approve() and detect.is_yes_no_shaped(perm_options):
                        tmux.send_keys(session, "1")  # the earlier key may have been dropped
                        perm_first_seen = time.monotonic()
                        log("auto_approve stuck retry: resent key 1")
                elif not perm and perm_hash:
                    if perm_question_id:
                        transport.set_project_status("running", pending_question_id="")
                        log("permission prompt gone (answered on the desktop)")
                    perm_question_id, perm_hash, perm_first_seen, perm_options = None, None, 0.0, []
                    flush_pending_message(transport, session, agent)  # a message queued behind the prompt
                elif not perm and not aq_hash and agent == "claude" and now - last_mismatch_warn >= 5.0:
                    if claude_pid is None:
                        claude_pid = resolve_claude_pid(session)
                    status = claude_session_status(claude_pid) if claude_pid else None
                    if status is None and claude_pid is not None:
                        claude_pid = None  # stale pid (e.g. after /restart)
                    elif status and status.get("status") == "waiting" and status.get("waitingFor") == "permission prompt":
                        log("Claude's session state says it waits on a permission prompt that the pane detector "
                            "did not see; possible detection bug", err=True)
                        last_mismatch_warn = now

                # ── Cursor's native confirmation widget ────────────────────
                if agent == "cursor":
                    cn = detect.detect_cursor_native_prompt(pane)
                    if cn and cn["hash"] != cn_hash:
                        cn_hash, cn_session_key, cn_first_seen = cn["hash"], cn["session_key"], time.monotonic()
                        if auto_approve():
                            tmux.send_keys(session, "y")
                            cn_question_id = None
                            log("Cursor native prompt auto-approved (y)")
                        elif is_mobile_mode(settings):
                            # The pre-tool hook may already have its own question for this same
                            # prompt; never clobber the single pending_question_id.
                            if not project_doc(transport).get("pending_question_id"):
                                cn_question_id = send_question(cn["question"], cn["options"], "permission",
                                                               "Cursor native confirmation prompt")
                                log(f"Cursor native prompt sent to the phone ({cn_question_id[:8]})")
                            else:
                                cn_question_id = None
                                log("Cursor native prompt: another question is pending; deferring to the stuck retry")
                        else:
                            cn_question_id = None
                            log("Cursor native prompt detected (desktop mode; not forwarding)")
                    elif cn and cn["hash"] == cn_hash:
                        if time.monotonic() - cn_first_seen >= 5.0 and auto_approve():
                            tmux.send_keys(session, "y")
                            cn_question_id, cn_first_seen = None, time.monotonic()
                            log("Cursor native prompt stuck retry: auto_approve is now true, sent y")
                    elif not cn and cn_hash and pane.strip():  # an empty capture is not "gone"
                        if cn_question_id:
                            transport.set_project_status("running", pending_question_id="")
                            log("Cursor native prompt gone (answered on the desktop)")
                        cn_question_id, cn_hash, cn_first_seen = None, None, 0.0

                # ── AskUserQuestion, single-select ─────────────────────────
                if not perm:
                    aq = detect.detect_ask_user_question(pane)
                    if aq and aq["hash"] != aq_hash:
                        aq_hash, aq_options = aq["hash"], aq["options"]
                        if is_mobile_mode(settings):
                            aq_question_id = send_question(aq["question"], aq["options"], "choice", "Claude Code question",
                                                           option_ids=[f"o{i}" for i in range(len(aq["options"]))])
                            log(f"AskUserQuestion sent to the phone ({aq_question_id[:8]})")
                        else:
                            aq_question_id = None
                            log("AskUserQuestion detected (desktop mode; not forwarding)")
                    elif not aq and aq_hash:
                        if aq_question_id:
                            transport.set_project_status("running", pending_question_id="")
                            log("AskUserQuestion gone (answered on the desktop)")
                        aq_question_id, aq_hash, aq_options = None, None, []

                # ── AskUserQuestion, multi-select: pick "Chat about this" for the phone ──
                if not perm:
                    ms = detect.detect_multiselect_prompt(pane)
                    if ms and ms["hash"] != ms_hash:
                        ms_hash = ms["hash"]
                        if is_mobile_mode(settings):
                            for _ in range(ms["down_presses"]):
                                tmux.send_keys(session, "Down")
                                time.sleep(0.15)
                            tmux.send_keys(session, "Enter")
                            log(f"multi-select question ({ms['label']}): auto-selected 'Chat about this' for the phone")
                        else:
                            log("multi-select question detected (desktop mode; not forwarding)")
                    elif not ms and ms_hash:
                        ms_hash = None

                # ── idle (Claude and Codex; not scoot, whose REPL is hook-managed) ──
                if (agent != "scoot" and not perm and not perm_hash and not aq_hash and not ms_hash and not cn_hash
                        and not trust_hash):
                    if detect.detect_idle_prompt(pane):
                        if idle_since is None:
                            idle_since = now
                        elif now - idle_since >= 1.5 and not idle_written:
                            try:
                                transport.set_project_status("idle", pending_question_id="")
                                idle_written = True
                                log("agent at the idle prompt; wrote idle")
                            except Exception as e:  # noqa: BLE001
                                log(f"idle status write failed: {e}", err=True)
                    else:
                        idle_since, idle_written = None, False

                # ── Claude API retry loop: no Stop hook fires while retrying ─
                if agent == "claude":
                    retry = detect.detect_claude_retry(pane)
                    if retry and retry["attempt"] >= detect.CLAUDE_RETRY_ALERT_ATTEMPT:
                        h = hashlib.md5(retry["message"].encode()).hexdigest()[:8]  # the text, never the attempt number
                        if h != claude_retry_hash:
                            claude_retry_hash = h
                            msg = (f"{retry['message']}: Claude is retrying automatically "
                                   f"(attempt {retry['attempt']} of {retry['total']}). No action needed unless it keeps failing.")
                            try:
                                transport.write_event("message", {"role": "assistant", "content": msg, "agent": "claude"})
                                transport.write_notification(msg, "warning", set_running=True)
                                log(f"Claude retry state sent to the phone: {retry['message'][:60]!r}")
                            except Exception as e:  # noqa: BLE001
                                log(f"Claude retry capture failed: {e}", err=True)
                    elif not retry and pane.strip():
                        claude_retry_hash = None

                # ── Gemini: no per-turn hook, capture the ✦ block when idle ──
                if agent == "gemini":
                    if detect.detect_gemini_idle(pane):
                        if gemini_idle_since is None:
                            gemini_idle_since = now
                        elif now - gemini_idle_since >= 1.5 and not gemini_idle_written:
                            response = detect.extract_gemini_response(pane)
                            if response:
                                h = hashlib.md5(response.encode()).hexdigest()[:8]
                                if h != gemini_last_hash:
                                    try:
                                        transport.write_event("message", {"role": "assistant", "content": response,
                                                                          "agent": "gemini"})
                                        gemini_last_hash = h
                                        log(f"gemini response captured ({len(response)} chars)")
                                    except Exception as e:  # noqa: BLE001
                                        log(f"gemini response write failed: {e}", err=True)
                                gemini_idle_written = True  # an empty block is retried next tick
                    else:
                        gemini_idle_since, gemini_idle_written = None, False

                # ── Cursor: idle fallback (its stop hook can go silent) + its error block ──
                if agent == "cursor":
                    if detect.detect_cursor_idle(pane):
                        if cursor_idle_since is None:
                            cursor_idle_since = now
                        elif now - cursor_idle_since >= 1.5:
                            if not cursor_idle_written:
                                try:
                                    transport.set_project_status("idle", pending_question_id="")
                                    log("Cursor at the idle prompt; wrote idle")
                                except Exception as e:  # noqa: BLE001
                                    log(f"Cursor idle status write failed: {e}", err=True)
                                cursor_idle_written = True
                            if now - cursor_resp_check_at >= 3.0:  # response capture is never gated by the flag
                                cursor_resp_check_at = now
                                try:
                                    t_path = detect.cursor_latest_transcript(str(project_dir))
                                    response = cursor_response_from_transcript(t_path) if t_path else ""
                                    log_path = settings.sessions_dir / f"{project_id}.jsonl"
                                    if response and response != detect.last_logged_assistant_message(log_path):
                                        transport.write_event("message", {"role": "assistant", "content": response,
                                                                          "agent": "cursor"})
                                        log(f"Cursor response captured via the idle fallback ({len(response)} chars)")
                                except Exception as e:  # noqa: BLE001
                                    log(f"Cursor fallback capture failed: {e}", err=True)
                    elif pane.strip():
                        cursor_idle_since, cursor_idle_written = None, False
                    cursor_err = detect.detect_cursor_error(pane)
                    if cursor_err:
                        h = hashlib.md5(cursor_err.encode()).hexdigest()[:8]
                        if h != cursor_error_hash:
                            cursor_error_hash = h
                            try:
                                transport.write_event("message", {"role": "assistant", "content": cursor_err, "agent": "cursor"})
                                transport.write_notification(cursor_err, "warning")
                                transport.set_project_status("stopped", pending_question_id="")
                                log(f"Cursor error sent to the phone: {cursor_err[:80]!r}")
                            except Exception as e:  # noqa: BLE001
                                log(f"Cursor error capture failed: {e}", err=True)
                    elif pane.strip():
                        cursor_error_hash = None

            # ── answers from the phone ─────────────────────────────────────
            if perm_question_id:
                answer = transport.poll_perm_answer_once(perm_question_id)
                if answer is not None:
                    try:
                        key = str(perm_options.index(answer) + 1)
                    except ValueError:
                        key = "1"
                    tmux.send_keys(session, key)  # a single digit, no Enter
                    transport.set_project_status("running", pending_question_id="")
                    set_mobile_mode(settings)
                    log(f"phone answered {answer!r}: sent key {key!r}")
                    perm_question_id, perm_options = None, []  # perm_hash stays until the pane is clean
                    flush_pending_message(transport, session, agent)

            if trust_question_id:
                answer = transport.poll_perm_answer_once(trust_question_id)
                if answer is not None:
                    if answer.lower().startswith("yes"):
                        tmux.send_keys(session, "Down")  # "No, exit" is preselected; move to "Yes"
                        time.sleep(0.2)
                    tmux.send_keys(session, "Enter")
                    transport.set_project_status("running", pending_question_id="")
                    set_mobile_mode(settings)
                    log(f"phone answered the trust dialog {answer!r}")
                    trust_question_id = None  # trust_hash stays until the dialog is gone
                    flush_pending_message(transport, session, agent)

            if cn_question_id:
                answer = transport.poll_perm_answer_once(cn_question_id)
                if answer is not None:
                    key = cn_session_key if answer == "Yes for this session" else detect.CURSOR_NATIVE_ANSWER_KEYS.get(answer, "n")
                    tmux.send_keys(session, key)
                    transport.set_project_status("running", pending_question_id="")
                    set_mobile_mode(settings)
                    log(f"phone answered {answer!r}: sent key {key!r} (Cursor native prompt)")
                    cn_question_id = None
                    flush_pending_message(transport, session, agent)

            if aq_question_id:
                answer = transport.poll_perm_answer_once(aq_question_id)
                if answer is not None:
                    idx = None  # by text, or by a stable "o<index>" id; never fall through to option 0
                    if answer in aq_options:
                        idx = aq_options.index(answer)
                    elif answer.startswith("o") and answer[1:].isdigit() and 0 <= int(answer[1:]) < len(aq_options):
                        idx = int(answer[1:])
                    if idx is None:
                        log(f"AskUserQuestion answer {answer!r} matched no option {aq_options}; ignored, re-prompting")
                        aq_question_id, aq_hash = None, None
                    else:
                        for _ in range(idx):  # each key its own call: bundled keys are not registered
                            tmux.send_keys(session, "Down")
                            time.sleep(0.15)
                        tmux.send_keys(session, "Enter")
                        transport.set_project_status("running", pending_question_id="")
                        set_mobile_mode(settings)
                        log(f"phone answered AskUserQuestion {answer!r}: Down x{idx} + Enter")
                        aq_question_id, aq_options = None, []
                        flush_pending_message(transport, session, agent)
        except Exception as e:  # noqa: BLE001 - the loop must survive any single tick
            log(f"detect error: {e}", err=True)

        # ── commands from the phone (every 2 s) ────────────────────────────
        if now - last_cmd_poll >= COMMAND_POLL_S and now >= backoff_until:
            last_cmd_poll = now
            try:
                commands = transport.poll_commands()
                for cmd in commands:  # execute, then mark done, per command (at-least-once)
                    try:
                        handle_command(cmd, transport, settings, session=session, agent=agent,
                                       project_dir=project_dir, project_id=project_id)
                        transport.mark_command_done(cmd.get("id"), ok=True)
                    except Exception as e:  # noqa: BLE001
                        log(f"command handler error for {str(cmd.get('id'))[:8]}: {e}", err=True)
                        transport.mark_command_done(cmd.get("id"), ok=False)
                if commands:
                    idle_since, idle_written = None, False  # a command just went in; not idle yet
                    # Any phone interaction re-checks a stuck permission prompt, bypassing the
                    # hash dedup, so the user can answer even after the first card expired.
                    stuck = detect.detect_permission_prompt(tmux.visible_pane(session))
                    if stuck:
                        if perm_question_id and stuck["hash"] == perm_hash:
                            if auto_approve() and detect.is_yes_no_shaped(stuck["options"]):
                                tmux.send_keys(session, "1")
                                transport.set_project_status("running", pending_question_id="")
                                perm_question_id, perm_hash = None, stuck["hash"]
                                log("auto_approve set while a question was pending: auto-approved")
                                flush_pending_message(transport, session, agent)
                            else:
                                log(f"stuck prompt already pending on the phone ({perm_question_id[:8]}); not resending")
                        elif auto_approve() and detect.is_yes_no_shaped(stuck["options"]):
                            tmux.send_keys(session, "1")
                            perm_hash, perm_question_id, perm_options = stuck["hash"], None, stuck["options"]
                            log("stuck prompt auto-approved (key 1)")
                            flush_pending_message(transport, session, agent)
                        else:
                            perm_question_id = send_question(stuck["question"], stuck["options"], "permission",
                                                             "Claude Code permission prompt")
                            perm_hash, perm_options = stuck["hash"], stuck["options"]
                            log(f"phone active: forwarded the stuck prompt ({perm_question_id[:8]})")
                backoff_until = 0.0
            except Exception as e:  # noqa: BLE001
                log(f"command poll error: {e}; retrying in 30 s", err=True)
                backoff_until = now + 30

        time.sleep(LOOP_SLEEP_S)

    # Do not write "stopped" here: if tmux vanished it was written in the loop, and a
    # daemon replaced by a newer one must not overwrite the newer one's status.
    log("session ending; updating computer status")
    try:
        transport.update_computer(status="offline")
    except Exception as e:  # noqa: BLE001
        log(f"could not update the stop status: {e}", err=True)
    transport.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aaw-daemon", description="Agents At Work Core session daemon")
    p.add_argument("--project-dir", metavar="PATH", required=True, help="the project folder the agent runs in")
    p.add_argument("--project-id", metavar="ID", default=None,
                   help="session identity (feed id, tmux name minus 'aaw-'); defaults to the folder basename")
    p.add_argument("--agent", metavar="NAME", default="claude", help="claude, codex, gemini, grok, cursor, or scoot")
    a = p.parse_args(argv)
    return run_session(load_settings(), Path(a.project_dir).resolve(), agent=a.agent, project_id=a.project_id)


if __name__ == "__main__":
    sys.exit(main())
