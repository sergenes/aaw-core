"""Hook registration: merges that never touch a user's own hooks, and a clean uninstall."""

from __future__ import annotations

import json
import sys

import pytest

from aaw_core.host import hooks_installer as hi


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "home", lambda: tmp_path)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return tmp_path


def test_hook_command_runs_this_interpreter():
    cmd = hi.hook_command("on_stop")
    assert cmd == f'"{sys.executable}" -m aaw_core.hooks.on_stop'
    assert hi.MARKER in cmd


def test_merge_replaces_our_entry_and_keeps_the_users(home):
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]},
                      {"hooks": [{"type": "command", "command": "python -m aaw_core.hooks.on_stop"}]}]}
    hi.merge_hook(hooks, "Stop", "on_stop", timeout=30)
    assert len(hooks["Stop"]) == 2
    assert hooks["Stop"][0]["hooks"][0]["command"] == "echo mine"
    assert hooks["Stop"][1]["hooks"][0] == {"type": "command", "command": hi.hook_command("on_stop"), "timeout": 30}
    hi.merge_hook(hooks, "PreToolUse", "on_pre_tool", matcher="*", timeout=360)
    assert hooks["PreToolUse"][0]["matcher"] == "*"


def test_claude_settings_merge_preserves_other_keys_and_is_idempotent(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [{"hooks": [{"command": "echo mine"}]}]}}))
    hi.update_claude_hooks()
    hi.update_claude_hooks()
    data = json.loads(path.read_text())
    assert data["theme"] == "dark"
    assert [h["hooks"][0]["command"] for h in data["hooks"]["Stop"]] == ["echo mine", hi.hook_command("on_stop")]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == hi.PRE_TOOL_TIMEOUT
    assert data["hooks"]["SessionStart"][0]["matcher"] == "clear"


def test_corrupt_settings_are_backed_up_not_clobbered(home, capsys):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("{not json")
    hi.update_claude_hooks()
    assert (home / ".claude" / "settings.json.corrupt.bak").read_text() == "{not json"
    assert "hooks" in json.loads(path.read_text())
    assert "backed up" in capsys.readouterr().err


def test_codex_hooks_and_feature_flag(home):
    hi.update_codex_hooks()
    cfg = (home / ".codex" / "config.toml").read_text()
    assert "[features]\nhooks = true" in cfg
    (home / ".codex" / "config.toml").write_text("[features]\ncodex_hooks = true\n")
    hi.update_codex_hooks()
    assert (home / ".codex" / "config.toml").read_text() == "[features]\nhooks = true\n"
    data = json.loads((home / ".codex" / "hooks.json").read_text())
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "*"


def test_scoot_keeps_its_file_shape(home):
    path = home / ".config" / "scoot" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"Stop": [{"hooks": [{"command": "echo mine"}]}]}))  # flat
    hi.update_scoot_hooks()
    data = json.loads(path.read_text())
    assert "hooks" not in data and len(data["Stop"]) == 2
    path.write_text(json.dumps({"hooks": {}, "other": 1}))  # nested
    hi.update_scoot_hooks()
    data = json.loads(path.read_text())
    assert data["other"] == 1 and "Stop" in data["hooks"]


def test_owned_files_and_remove_all(home, monkeypatch):
    monkeypatch.setattr(hi, "is_installed", lambda agent: True)
    hi.install_all(log=lambda m: None)
    grok = home / ".grok" / "hooks" / "aaw-core.json"
    cursor = home / ".cursor" / "hooks.json"
    assert json.loads(grok.read_text())["hooks"]["PreToolUse"][0]["env"] == {"AAW_AGENT": "grok"}
    assert json.loads(cursor.read_text())["version"] == 1
    # a user hook next to ours survives the uninstall; a file that was only ours goes away
    claude = home / ".claude" / "settings.json"
    data = json.loads(claude.read_text())
    data["hooks"]["Stop"].insert(0, {"hooks": [{"command": "echo mine"}]})
    claude.write_text(json.dumps(data))
    touched = hi.remove_all()
    assert str(claude) in touched and str(grok) in touched and str(cursor) in touched
    assert not grok.exists() and not cursor.exists()
    left = json.loads(claude.read_text())
    assert left["hooks"] == {"Stop": [{"hooks": [{"command": "echo mine"}]}]}
    assert not (home / ".codex" / "hooks.json").exists()  # only our hooks were in it
    assert hi.remove_all() == []


def test_foreign_owned_file_is_not_deleted(home):
    cursor = home / ".cursor" / "hooks.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "echo theirs"}]}}))
    assert hi.remove_all() == []
    assert cursor.exists()


def test_shell_integration_round_trip(home):
    rc = home / ".zshrc"
    rc.write_text("export A=1")  # no trailing newline
    assert hi.shell_integration_status() is False
    assert hi.add_shell_integration() is True
    assert hi.add_shell_integration() is False
    text = rc.read_text()
    assert text.startswith("export A=1\n\n") and hi.SHELL_LINE in text and hi.SHELL_COMMENT in text
    assert hi.shell_integration_status() is True
    assert hi.remove_shell_integration() is True
    assert rc.read_text() == "export A=1\n\n"
    assert hi.remove_shell_integration() is False


def test_extend_path_prepends_existing_user_bins(home, monkeypatch):
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin")
    hi.extend_path()
    import os
    assert os.environ["PATH"].split(os.pathsep)[0] == str(home / ".local" / "bin")
    assert os.environ["PATH"].endswith("/usr/bin")
