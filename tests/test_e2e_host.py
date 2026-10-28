"""End to end on this machine: a real relay, the real daemon as a subprocess, a real tmux
session running a fake agent, and a simulated phone. Skipped where tmux is missing.

The fake agent is a shell loop that echoes what it is told, so the test proves the host
pipeline (session start, daemon registration, a phone prompt typed into the pane and
echoed to the feed, stop detection) without depending on a real agent's UI."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

import pytest
from test_relay import _phone_connect, _poll_until, _recv_until, live_relay  # noqa: F401 (fixture)

from aaw_core.config import load_settings
from aaw_core.encryption import decrypt, load_or_create_key
from aaw_core.hooks.common import tmux_session
from aaw_core.host import sessions, tmux
from aaw_core.host.identity import load_or_create_identity
from aaw_core.transport.base import make_command
from aaw_core.transport.relay import RelayTransport

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

FAKE_AGENT = """#!/usr/bin/env bash
echo "fake claude ready"
while IFS= read -r line; do
  echo "you said: $line"
  [ "$line" = "/exit" ] && exit 0
done
"""
SESSION_ID = "e2e"


@pytest.fixture
def host(tmp_path, monkeypatch, live_relay):  # noqa: F811 - the imported fixture
    # A private tmux server (short socket path: macOS caps it at 104 bytes) so the test
    # never touches the user's own sessions.
    tmux_dir = tempfile.mkdtemp(prefix="aaw-e2e-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", tmux_dir)
    monkeypatch.delenv("TMUX", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_AGENT)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))  # an empty ~/.claude: no --continue
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AAW_RELAY_URL", live_relay)
    monkeypatch.setenv("AAW_KEEP_AWAKE", "false")
    settings = load_settings()
    settings.state_dir.mkdir()
    ident = load_or_create_identity(settings)
    key = load_or_create_key(settings.session_key_file)
    settings.enabled_flag.write_text("1")
    project = tmp_path / "proj"
    project.mkdir()
    try:
        yield settings, ident, key, project, live_relay
    finally:
        tmux.tmux_run(["kill-session", "-t", tmux_session(SESSION_ID)], capture_output=True)
        sessions.stop_daemon(settings, SESSION_ID, grace=0.5)
        tmux.tmux_run(["kill-server"], capture_output=True)
        shutil.rmtree(tmux_dir, ignore_errors=True)


def test_session_bridges_prompts_and_reports_stop(host):
    settings, ident, key, project, relay_url = host
    lines = sessions.start_session(settings, project, SESSION_ID, "claude")
    assert any("daemon started" in ln for ln in lines), lines
    session = tmux_session(SESSION_ID)
    assert tmux.has_session(session)
    assert sessions.session_agent(SESSION_ID) == "claude"
    assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)

    reader = RelayTransport(relay_url=relay_url, token=ident.token, computer_id=ident.computer_id,
                            project_id="_test", sessions_dir=settings.sessions_dir, enc_key=key).start()
    try:
        assert reader.wait_connected(10)

        # the daemon registers the project on the relay, with its path encrypted
        def registered():
            doc = reader.list_projects().get(SESSION_ID) or {}
            return doc if doc.get("status") in ("running", "idle") and doc.get("project_path") else None
        doc = _poll_until(registered, timeout=30)
        assert doc["agent"] == "claude"
        assert decrypt(doc["project_path"], key) == str(project)
        assert sessions.daemon_pid(settings, SESSION_ID)

        # a prompt from the phone is typed into the pane, consumed, and echoed to the feed
        phone = _phone_connect(relay_url, reader.register_phone_token())
        phone.send(json.dumps({"type": "subscribe", "project_id": SESSION_ID}))
        cmd = make_command("hello from the phone", key, source="phone")
        phone.send(json.dumps({"type": "command", "project_id": SESSION_ID, "command": cmd}))
        assert _poll_until(lambda: "you said: hello from the phone" in tmux.visible_pane(session), timeout=30)
        # the daemon echoes the prompt to the feed first, then marks the command consumed
        ev = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "message", timeout=30)
        assert decrypt(ev["event"]["payload"]["content"], key) == "hello from the phone"
        upd = _recv_until(phone, lambda f: f.get("type") == "command_update" and f["command_id"] == cmd["id"],
                          timeout=30)
        assert upd["fields"]["consumed"] is True
        local = (settings.sessions_dir / f"{SESSION_ID}.jsonl").read_text()
        assert '"content": "hello from the phone"' in local  # plaintext stays on the computer

        # the agent goes away: the daemon writes "stopped" on its own and exits
        tmux.tmux_run(["kill-session", "-t", session], capture_output=True)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status") == "stopped",
                           timeout=45)
        assert _poll_until(lambda: sessions.daemon_pid(settings, SESSION_ID) is None, timeout=30)
        phone.close()
    finally:
        reader.stop(flush_timeout=0)


def test_a_prompt_sent_during_the_stopped_gap_lands_on_restart(host):
    """The 2026-09-29 incident: /exit killed the session, a phone prompt arrived in the
    gap before the user restarted, and the restarted session never saw it. The feed said
    sent; the terminal said nothing. A prompt sent while no daemon is alive must land
    exactly once in the next session for that folder (the relay replays unconsumed
    commands to every new computer connection)."""
    settings, ident, key, project, relay_url = host
    sessions.start_session(settings, project, SESSION_ID, "claude")
    session = tmux_session(SESSION_ID)
    assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)

    reader = RelayTransport(relay_url=relay_url, token=ident.token, computer_id=ident.computer_id,
                            project_id="_test", sessions_dir=settings.sessions_dir, enc_key=key).start()
    try:
        assert reader.wait_connected(10)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status")
                           in ("running", "idle"), timeout=30)

        # the session dies and its daemon exits: the gap begins
        tmux.tmux_run(["kill-session", "-t", session], capture_output=True)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status") == "stopped",
                           timeout=45)
        assert _poll_until(lambda: sessions.daemon_pid(settings, SESSION_ID) is None, timeout=30)

        # a prompt arrives while nothing is alive to deliver it
        phone = _phone_connect(relay_url, reader.register_phone_token())
        cmd = make_command("missed while stopped", key, source="phone")
        phone.send(json.dumps({"type": "command", "project_id": SESSION_ID, "command": cmd}))

        def stored_unconsumed():
            docs = reader.list_commands(SESSION_ID)
            return any(d.get("id") == cmd["id"] and not d.get("consumed") for d in docs)
        assert _poll_until(stored_unconsumed, timeout=15)

        # the user restarts the session: the prompt must land exactly once
        sessions.start_session(settings, project, SESSION_ID, "claude")
        assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)
        assert _poll_until(lambda: tmux.visible_pane(session).count("you said: missed while stopped") == 1,
                           timeout=30)

        def consumed():
            docs = reader.list_commands(SESSION_ID)
            return all(d.get("consumed") for d in docs if d.get("id") == cmd["id"])
        assert _poll_until(consumed, timeout=30)

        # a prompt that waited past the cap is dropped with a feed notice, not typed:
        # same gap, but the command's ts says it has been waiting over STALE_COMMAND_S
        from aaw_core.daemon import STALE_COMMAND_S
        from aaw_core.transport.base import now_ms
        tmux.tmux_run(["kill-session", "-t", session], capture_output=True)
        assert _poll_until(lambda: sessions.daemon_pid(settings, SESSION_ID) is None, timeout=30)
        old = make_command("too late to matter", key, source="phone")
        old["ts"] = now_ms() - (STALE_COMMAND_S + 120) * 1000
        phone.send(json.dumps({"type": "command", "project_id": SESSION_ID, "command": old}))
        assert _poll_until(lambda: any(d.get("id") == old["id"] for d in reader.list_commands(SESSION_ID)),
                           timeout=15)
        phone.send(json.dumps({"type": "subscribe", "project_id": SESSION_ID}))
        sessions.start_session(settings, project, SESSION_ID, "claude")
        assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)
        notice = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "notification"
                             and "Not delivered" in decrypt(f["event"]["payload"]["message"], key), timeout=30)
        assert "too late to matter" in decrypt(notice["event"]["payload"]["message"], key)
        assert "you said: too late to matter" not in tmux.visible_pane(session)
        assert _poll_until(lambda: all(d.get("consumed") for d in reader.list_commands(SESSION_ID)
                                       if d.get("id") == old["id"]), timeout=30)
        phone.close()
    finally:
        reader.stop(flush_timeout=0)


MENU_THEN_PROMPT_AGENT = r"""#!/usr/bin/env bash
echo "fake claude ready"
read -r line
printf '\033[2J\033[H'
printf '⏺ Bash(git commit -m "earlier work")\n  ⎿  Done\n✻ Brewed for 9s\n❯ /model\n'
printf '────────────────────────────────────────\n  Select model\n'
printf '  Switch between Claude models. Your pick becomes the default for new sessions.\n\n'
printf '  ❯ 1. Default (recommended)  Opus 5.5\n    2. Fable 5.1              For your toughest challenges\n'
printf '    3. Sonnet 5.5             Most efficient for simpler tasks\n\n'
printf '  Enter to set as default · s to use this session only · Esc to cancel\n'
read -r line
# One write: the model menu is replaced by a tool approval between two daemon polls,
# so the daemon never sees a pane without a menu (the 2026-10-23 stuck card).
printf '\033[2J\033[H⏺ Bash(touch hello.txt)\n  ⎿  Waiting…\n────────────────────────────────────────\n Bash command\n touch hello.txt\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend\n'
read -r -n1 key  # Claude takes the answer as a single keypress
echo "you said: $key"
while IFS= read -r line; do echo "you said: $line"; done
"""


def test_a_menu_replaced_by_an_auto_approved_prompt_leaves_no_card(host):
    """2026-10-23: /model opened mid-turn reached the phone as a "permission prompt" titled
    with the agent's last git command, and when the next tool approval replaced it and
    auto-approve answered that, the model card stayed on the phone until the turn ended.
    The menu must arrive as a choice titled by its header, and the card must clear when
    the menu it belongs to is replaced."""
    settings, ident, key, project, relay_url = host
    fake = shutil.which("claude")
    with open(fake, "w") as f:
        f.write(MENU_THEN_PROMPT_AGENT)
    settings.mobile_mode_file.write_text("manual")
    sessions.start_session(settings, project, SESSION_ID, "claude")
    session = tmux_session(SESSION_ID)
    assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)

    reader = RelayTransport(relay_url=relay_url, token=ident.token, computer_id=ident.computer_id,
                            project_id="_test", sessions_dir=settings.sessions_dir, enc_key=key).start()
    try:
        assert reader.wait_connected(10)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status")
                           in ("running", "idle"), timeout=30)
        phone = _phone_connect(relay_url, reader.register_phone_token())
        phone.send(json.dumps({"type": "subscribe", "project_id": SESSION_ID}))
        phone.send(json.dumps({"type": "state", "project_id": SESSION_ID, "fields": {"auto_approve": True}}))
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("auto_approve"), timeout=15)

        # the user opens /model: a choice card titled by the menu, never auto-answered
        tmux.send_keys(session, "go", "Enter")
        ev = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "question",
                         timeout=30)
        payload = ev["event"]["payload"]
        assert payload["kind"] == "choice"
        assert decrypt(payload["question"], key) == "Select model"
        card = ev["event"]["id"]
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("pending_question_id")
                           == card, timeout=15)
        assert "you said" not in tmux.visible_pane(session)  # no key went into the menu

        # the menu gives way to a Yes/No approval in one screen write; auto-approve answers it
        tmux.send_keys(session, "next", "Enter")
        assert _poll_until(lambda: "you said: 1" in tmux.visible_pane(session), timeout=30)
        assert _poll_until(lambda: not (reader.list_projects().get(SESSION_ID) or {}).get("pending_question_id"),
                           timeout=15)
        phone.close()
    finally:
        reader.stop(flush_timeout=0)


# Claude Code 2.1.312 during an API retry: Enter queues the prompt and the input box shows
# a placeholder until the turn frees up (captured 2026-10-28 against a dead API address).
RETRY_QUEUE_AGENT = r"""#!/usr/bin/env bash
RULE=$(printf '─%.0s' $(seq 1 60))
echo "fake claude ready"
printf '\033[2J\033[H❯ say hi\n✻ Unable to connect to API (EPROTO) · Retrying in 9s · attempt 3/10\n%s\n❯ \n%s\n' "$RULE" "$RULE"
read -r line
printf '\033[2J\033[H❯ say hi\n❯ %s\n  ctrl+x ctrl+s to send now\n' "$line"
printf '✻ Unable to connect to API (EPROTO) · Retrying in 8s · attempt 3/10\n%s\n❯ Press up to edit queued messages\n%s\n' "$RULE" "$RULE"
while IFS= read -r more; do :; done
"""


def test_a_prompt_sent_during_an_api_retry_is_delivered_not_reported_lost(host):
    """2026-10-26: Claude was retrying its API, every phone prompt was queued by Claude
    and later ran, yet each one also got "Your message did not reach the session": the
    daemon read the queued-prompt placeholder as text left in the input box. A queued
    prompt is delivered: echoed to the feed once, and no undelivered notice."""
    settings, ident, key, project, relay_url = host
    with open(shutil.which("claude"), "w") as f:
        f.write(RETRY_QUEUE_AGENT)
    sessions.start_session(settings, project, SESSION_ID, "claude")
    session = tmux_session(SESSION_ID)
    assert _poll_until(lambda: "Retrying in 9s" in tmux.visible_pane(session), timeout=15)

    reader = RelayTransport(relay_url=relay_url, token=ident.token, computer_id=ident.computer_id,
                            project_id="_test", sessions_dir=settings.sessions_dir, enc_key=key).start()
    try:
        assert reader.wait_connected(10)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status")
                           in ("running", "idle"), timeout=30)
        phone = _phone_connect(relay_url, reader.register_phone_token())
        phone.send(json.dumps({"type": "subscribe", "project_id": SESSION_ID}))
        cmd = make_command("Status", key, source="phone")
        phone.send(json.dumps({"type": "command", "project_id": SESSION_ID, "command": cmd}))
        assert _poll_until(lambda: "Press up to edit queued messages" in tmux.visible_pane(session), timeout=30)
        ev = _recv_until(phone, lambda f: f.get("type") == "event" and f["event"]["type"] == "message"
                         and f["event"]["payload"].get("role") == "user", timeout=30)
        assert decrypt(ev["event"]["payload"]["content"], key) == "Status"
        # the old code re-sent Enter five times (about 8 s) before reporting it lost
        time.sleep(10)
        local = (settings.sessions_dir / f"{SESSION_ID}.jsonl").read_text()
        assert "did not reach" not in local
        assert '"content": "Status"' in local
        phone.close()
    finally:
        reader.stop(flush_timeout=0)


# Claude Code 2.1.312 with an expired login: the turn ends at once with a ⏺ reply, and
# Claude runs its StopFailure hook (not Stop) with the error named in the payload.
STOP_FAILURE_AGENT = r"""#!/usr/bin/env bash
echo "fake claude ready"
while IFS= read -r line; do
  printf '❯ %s\n⏺ Please run /login · API Error: 401 Invalid bearer token\n' "$line"
  printf '{"hook_event_name": "StopFailure", "error": "authentication_failed", "last_assistant_message": "Please run /login · API Error: 401 Invalid bearer token"}' \
    | "__PYTHON__" -m aaw_core.hooks.on_stop_failure
done
"""


def test_a_turn_that_dies_on_an_expired_login_tells_the_phone_once(host):
    """2026-10-26: Claude's login expired and every phone prompt got "Login expired ·
    Please run /login" in the pane while the phone heard nothing, because Claude reports
    an API error through StopFailure, which nothing listened to. The phone must get one
    push saying to sign in again, and each failed prompt still shows in the feed."""
    settings, ident, key, project, relay_url = host
    with open(shutil.which("claude"), "w") as f:
        f.write(STOP_FAILURE_AGENT.replace("__PYTHON__", sys.executable))
    sessions.start_session(settings, project, SESSION_ID, "claude")
    session = tmux_session(SESSION_ID)
    assert _poll_until(lambda: "fake claude ready" in tmux.visible_pane(session), timeout=15)

    reader = RelayTransport(relay_url=relay_url, token=ident.token, computer_id=ident.computer_id,
                            project_id="_test", sessions_dir=settings.sessions_dir, enc_key=key).start()
    try:
        assert reader.wait_connected(10)
        assert _poll_until(lambda: (reader.list_projects().get(SESSION_ID) or {}).get("status")
                           in ("running", "idle"), timeout=30)
        phone = _phone_connect(relay_url, reader.register_phone_token())
        phone.send(json.dumps({"type": "subscribe", "project_id": SESSION_ID}))
        for n, text in enumerate(("Hey", "Status"), start=1):
            cmd = make_command(text, key, source="phone")
            phone.send(json.dumps({"type": "command", "project_id": SESSION_ID, "command": cmd}))
            assert _poll_until(lambda n=n: tmux.visible_pane(session).count("401 Invalid bearer token") == n,
                               timeout=30)
        log = settings.sessions_dir / f"{SESSION_ID}.jsonl"
        assert _poll_until(lambda: log.read_text().count("sign back in") >= 3, timeout=30)
        events = [json.loads(ln) for ln in log.read_text().splitlines()]
        pushes = [e for e in events if e["type"] == "notification" and "sign back in" in e["message"]]
        feed = [e for e in events if e["type"] == "message" and "sign back in" in e.get("content", "")]
        assert len(pushes) == 1 and pushes[0]["level"] == "error"
        assert len(feed) == 2 and all("⏺" not in e["content"] for e in feed)
        phone.close()
    finally:
        reader.stop(flush_timeout=0)
