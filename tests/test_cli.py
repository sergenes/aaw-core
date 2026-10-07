"""The aaw command line: time parsing, the feed renderer, the QR payload, and the parser."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aaw_core.config import Settings
from aaw_core.host import cli
from aaw_core.host.identity import load_identity

TZ = timezone(timedelta(hours=-4))
NOW = datetime(2026, 9, 15, 21, 30, tzinfo=TZ)


@pytest.mark.parametrize("text,expected", [
    ("22:00", (22, 0)), ("8:05", (8, 5)), ("10pm", (22, 0)), ("10:30 PM", (22, 30)), ("12am", (0, 0)),
    ("12pm", (12, 0)), ("12:15 a.m.", (0, 15)), ("8", None), ("24:00", None), ("13pm", None), ("x", None),
])
def test_parse_time_of_day(text, expected):
    assert cli.parse_time_of_day(text) == expected


def test_parse_when_relative_next_occurrence_and_absolute():
    assert cli.parse_when("+90m", NOW) == int((NOW + timedelta(minutes=90)).timestamp() * 1000)
    assert cli.parse_when("+3h", NOW) == int((NOW + timedelta(hours=3)).timestamp() * 1000)
    later_today = cli.parse_when("22:00", NOW)
    assert later_today == int(NOW.replace(hour=22, minute=0).timestamp() * 1000)
    tomorrow = cli.parse_when("8pm", NOW)  # already past today
    assert tomorrow == int((NOW + timedelta(days=1)).replace(hour=20, minute=0).timestamp() * 1000)
    absolute = cli.parse_when("2026-09-16 10:30 PM", NOW)
    assert absolute == int(NOW.replace(day=16, hour=22, minute=30).timestamp() * 1000)
    with pytest.raises(ValueError, match="in the past"):
        cli.parse_when("2026-09-15 20:00", NOW)
    with pytest.raises(ValueError, match="couldn't parse"):
        cli.parse_when("tomorrow", NOW)
    assert cli.fmt_when(absolute).endswith(":30")


def test_render_feed_wraps_and_labels(monkeypatch):
    entries = [
        {"type": "message", "role": "user", "ts": 0, "content": "hi"},
        {"type": "message", "role": "assistant", "ts": 0, "content": "word " * 30},
        {"type": "question", "ts": 0, "question": "Allow?", "options": ["Yes", "No"]},
        {"type": "notification", "ts": 0, "message": "done"},
        {"type": "unknown", "ts": 0},
    ]
    text = cli.render_feed(entries, 40, tty=False)
    assert "] user\n  hi" in text
    assert "] QUESTION\n  Allow?\n  options: Yes, No" in text
    assert "] note\n  done" in text
    assert all(len(line) <= 40 for line in text.splitlines())
    colored = cli.render_feed(entries[:1], 40, tty=True)
    assert "\033[36m" in colored


def test_qr_payload_creates_identity_and_key(tmp_path):
    settings = Settings(state_dir=tmp_path, relay_url="wss://relay.example/v1/ws", computer_name="box",
                        keep_awake=False)
    payload = json.loads(cli.qr_payload(settings, "phone-token"))
    ident = load_identity(settings)
    assert payload == {"v": 1, "computer_id": ident.computer_id, "name": "box",
                       "relay_url": "wss://relay.example/v1/ws", "key": settings.session_key_file.read_text().strip(),
                       "token": "phone-token"}
    assert oct(settings.session_key_file.stat().st_mode & 0o777) == "0o600"


def test_deep_link_round_trips_the_qr_payload(tmp_path):
    import base64
    settings = Settings(state_dir=tmp_path, relay_url="wss://relay.example/v1/ws", computer_name="box",
                        keep_awake=False)
    payload = cli.qr_payload(settings, "phone-token")
    link = cli.deep_link(payload)
    assert link.startswith("agentsatwork://link?d=")
    enc = link.split("d=", 1)[1]
    # a url-safe-base64 value never needs escaping in a URL (the app re-pads before decoding)
    assert "/" not in enc and "+" not in enc and "=" not in enc
    decoded = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode()
    assert json.loads(decoded) == json.loads(payload)


def test_configure_relay_offers_the_hosted_relay_and_saves_the_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AAW_RELAY_URL", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    settings = cli.load_settings()
    assert settings.relay_url is None
    answers = iter([""])  # Enter = the hosted relay
    settings = cli.configure_relay(settings, ask=lambda prompt: next(answers))
    assert settings.relay_url == cli.HOSTED_RELAY_URL
    assert json.loads((tmp_path / "config.json").read_text()) == {"relay_url": cli.HOSTED_RELAY_URL}

    answers = iter(["2", "wss://relay.example/v1/ws"])
    settings = cli.configure_relay(settings, ask=lambda prompt: next(answers))
    assert settings.relay_url == "wss://relay.example/v1/ws"
    with pytest.raises(SystemExit):
        cli.configure_relay(settings, url="https://not-a-relay")
    settings = cli.configure_relay(settings, url="ws://192.168.1.5:8765/v1/ws")  # --relay, a LAN test
    assert settings.relay_url == "ws://192.168.1.5:8765/v1/ws"


def test_parser_covers_the_commands():
    p = cli.build_parser()
    a = p.parse_args(["start", "~/proj", "--agent", "codex", "--no-attach"])
    assert (a.fn, a.agent, a.no_attach) == (cli.cmd_start, "codex", True)
    a = p.parse_args(["schedule", "proj", "run it", "--at", "10pm"])
    assert (a.fn, a.at) == (cli.cmd_schedule, "10pm")
    a = p.parse_args(["scheduled", "proj", "new text", "--edit", "2", "--at", "+3h"])
    assert (a.edit, a.text) == (2, "new text")
    for name in ("link", "status", "quit", "supervisor", "shell-init", "install-hooks"):
        assert p.parse_args([name]).fn is not None
    assert p.parse_args(["service", "install"]).fn is cli.cmd_service
    with pytest.raises(SystemExit):
        p.parse_args(["start", "~/proj", "--agent", "ollama"])


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("aaw-core ")


def test_shell_init_prints_the_functions(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("AAW_STATE_DIR", str(tmp_path))
    assert cli.main(["shell-init"]) == 0
    out = capsys.readouterr().out
    assert "claude()" in out and "cursor-agent()" in out and "aaw start" in out


def test_status_json_reads_the_mirror_and_host_state(tmp_path, monkeypatch, capsys):
    from aaw_core.config import Settings
    from aaw_core.host import sessions, state
    settings = Settings(state_dir=tmp_path, relay_url="wss://r/v1/ws", computer_name="box", keep_awake=False)
    state.merge_project(state.mirror_dir(tmp_path), "proj", {"status": "running", "agent": "claude"})
    tmp_path.joinpath("mobile_mode").write_text("manual")
    monkeypatch.setattr(sessions, "list_sessions", lambda: [sessions.Session(id="aaw-proj", project="proj", path="/p", attached=False)])
    monkeypatch.setattr(sessions, "session_agent", lambda p: "claude")
    monkeypatch.setattr(sessions, "daemon_pid", lambda s, p: 4242)
    a = cli.build_parser().parse_args(["status", "--json"])
    cli.cmd_status(a, settings)
    out = json.loads(capsys.readouterr().out)
    assert out["sessions"] == [{"id": "proj", "agent": "claude", "path": "/p", "daemon_pid": 4242}]
    assert out["projects"]["proj"]["status"] == "running"
    assert out["mobile_mode"] == "manual" and out["linked"] is False and out["relay_url"] == "wss://r/v1/ws"
    assert out["supervisor"] == {"enabled": False, "enabled_at": 0, "fresh": False}


def test_parser_json_flags():
    p = cli.build_parser()
    assert p.parse_args(["link", "--json"]).json
    assert p.parse_args(["status", "--json", "--agents"]).agents
    assert p.parse_args(["scheduled", "proj", "--json"]).json
    assert p.parse_args(["models", "--json"]).json


def test_identity_honours_a_migrated_computer_id(tmp_path, monkeypatch):
    from aaw_core.config import Settings
    from aaw_core.host.identity import load_or_create_identity
    monkeypatch.setenv("AAW_COMPUTER_ID", "OLD-MAC-ID")
    settings = Settings(state_dir=tmp_path, relay_url="wss://r", computer_name="mac", keep_awake=False)
    ident = load_or_create_identity(settings)
    assert ident.computer_id == "OLD-MAC-ID" and ident.token
    monkeypatch.setenv("AAW_COMPUTER_ID", "OTHER")
    assert load_or_create_identity(settings).computer_id == "OLD-MAC-ID"  # persisted, never re-minted


def test_scheduled_parser_by_id():
    p = cli.build_parser()
    a = p.parse_args(["scheduled", "proj", "--id", "cmd-1"])
    assert a.by_id == "cmd-1" and a.delete is None
    a = p.parse_args(["scheduled", "proj", "new text", "--id", "cmd-1", "--edit", "0", "--at", "+1h"])
    assert (a.by_id, a.edit, a.text) == ("cmd-1", 0, "new text")


def test_module_entry_point_runs(tmp_path):
    """`python -m aaw_core.host.cli` is how an embedding app (the Mac app) drives the host."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "aaw_core.host.cli", "status", "--json"],
                       env={**__import__("os").environ, "AAW_STATE_DIR": str(tmp_path)},
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["linked"] is False


def test_link_json_prints_only_the_payload(tmp_path, monkeypatch, capsys):
    """A GUI reads stdout as the payload; the first link on a fresh install must not add lines."""
    from aaw_core.config import Settings
    settings = Settings(state_dir=tmp_path, relay_url=None, computer_name="box", keep_awake=False)
    monkeypatch.setattr(cli, "_transport", lambda s, p: type("T", (), {
        "register_phone_token": lambda self: "tok", "flush": lambda self, t: True, "stop": lambda self, **k: None})())
    a = cli.build_parser().parse_args(["link", "--json"])
    cli.cmd_link(a, settings)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0])["token"] == "tok"
