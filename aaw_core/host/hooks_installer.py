"""Register the hooks with each installed agent, and the shell integration.

  Claude Code  ~/.claude/settings.json          merge into "hooks"
  Codex CLI    ~/.codex/hooks.json              merge into "hooks", plus [features] hooks = true
  Gemini CLI   ~/.gemini/settings.json          merge into "hooks"
  scoot        ~/.config/scoot/hooks.json       merge (flat or nested "hooks")
  Grok CLI     ~/.grok/hooks/aaw-core.json      a file we own
  Cursor CLI   ~/.cursor/hooks.json             merge into "hooks" (flat entries per event)

Every hook command is ``"<this python>" -m aaw_core.hooks.<name>``, so the entries
are recognizable (``-m aaw_core.hooks.``) whatever the install location. Merging
replaces an existing entry for the same hook and appends otherwise, so a user's own
hooks are never touched. Uninstall strips only our entries.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from aaw_core.host.sessions import AGENT_BINARIES

MARKER = "-m aaw_core.hooks."

# PreToolUse 360 s: on_pre_tool blocks up to 300 s polling the phone; an agent's default
# (60 s) would kill it mid-approval, orphaning the question on the phone.
PRE_TOOL_TIMEOUT = 360


def home() -> Path:
    return Path.home()


def hook_command(name: str) -> str:
    return f'"{sys.executable}" -m aaw_core.hooks.{name}'


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        # The file exists but is not JSON. Returning {} would let the caller overwrite
        # whatever the user had; back it up first so nothing is lost.
        backup = path.with_name(path.name + ".corrupt.bak")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        print(f"[hooks] {path} is not valid JSON, backed up to {backup.name} before rewriting ({e})",
              file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    """Atomic write (unique temp + rename): a crash mid-write can never leave a half-written
    config that the next read would treat as corrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_hook(hooks: dict, event: str, name: str, *, matcher: str | None = None,
               timeout: int | None = None) -> None:
    """One entry per hook per event: replace the group that already runs this hook, else append."""
    ident = f"{MARKER}{name}"
    hook_dict: dict = {"type": "command", "command": hook_command(name)}
    if timeout is not None:
        hook_dict["timeout"] = timeout
    entry: dict = {"hooks": [hook_dict]}
    if matcher:
        entry["matcher"] = matcher
    event_list = list(hooks.get(event) or [])
    for i, group in enumerate(event_list):
        cmds = [h.get("command") or "" for h in (group.get("hooks") or []) if isinstance(h, dict)]
        if any(ident in c for c in cmds):
            event_list[i] = entry
            break
    else:
        event_list.append(entry)
    hooks[event] = event_list


def user_bin_dirs() -> list[Path]:
    """Where the agent CLIs land: Claude's and Cursor's installers use ~/.local/bin, npm -g
    uses ~/.npm-global/bin or the current nvm node, bun uses ~/.bun/bin. A systemd user
    service and a non-login shell often have none of these on PATH."""
    h = home()
    dirs = [h / ".local/bin", h / ".npm-global/bin", h / ".bun/bin",
            h / ".claude/bin", h / ".cursor/bin", h / ".codex/bin", h / ".gemini/bin"]
    dirs += sorted(h.glob(".nvm/versions/node/*/bin"), reverse=True)
    dirs += [Path("/usr/local/bin"), Path("/snap/bin"), Path("/opt/homebrew/bin")]
    return [d for d in dirs if d.is_dir()]


def extend_path() -> None:
    """Prepend the user bin dirs missing from PATH, for this process and everything it starts
    (tmux servers, daemons). A remote start from a systemd service otherwise runs `claude` in
    a pane whose PATH lacks it: "command not found", session stopped at once."""
    current = os.environ.get("PATH", "").split(os.pathsep)
    missing = [str(d) for d in user_bin_dirs() if str(d) not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + [p for p in current if p])


def is_installed(agent: str) -> bool:
    binary = AGENT_BINARIES.get(agent, agent)
    if shutil.which(binary):
        return True
    return any((d / binary).exists() for d in user_bin_dirs())


def detected_agents() -> list[str]:
    """What the heartbeat reports; Claude is listed even when absent (the default agent)."""
    found = [a for a in ("claude", "codex", "gemini", "grok", "cursor", "scoot") if is_installed(a)]
    return found or ["claude"]


# ── per-agent files ───────────────────────────────────────────────────────────


def _claude_shaped(hooks: dict, *, session_start_matcher: str | None) -> None:
    merge_hook(hooks, "Stop", "on_stop", timeout=30)
    merge_hook(hooks, "Notification", "on_notification", timeout=10)
    merge_hook(hooks, "PreToolUse", "on_pre_tool", timeout=PRE_TOOL_TIMEOUT)
    merge_hook(hooks, "PostToolUse", "on_post_tool", timeout=10)
    merge_hook(hooks, "UserPromptSubmit", "on_user_prompt", timeout=10)
    merge_hook(hooks, "SessionStart", "on_session_start", matcher=session_start_matcher, timeout=10)


def update_claude_hooks() -> Path:
    path = home() / ".claude" / "settings.json"
    settings = _read_json(path)
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    _claude_shaped(hooks, session_start_matcher="clear")
    # Claude only: the running-subagents set behind "Waiting for N background agents".
    merge_hook(hooks, "SubagentStart", "on_subagent", timeout=10)
    merge_hook(hooks, "SubagentStop", "on_subagent", timeout=10)
    settings["hooks"] = hooks
    _write_json(path, settings)
    return path


def update_scoot_hooks() -> Path:
    """scoot accepts Claude-shaped hook payloads and decisions; its file is flat
    ({event: [...]}) or nested under "hooks", and we keep whichever shape it has."""
    path = home() / ".config" / "scoot" / "hooks.json"
    data = _read_json(path)
    nested = isinstance(data.get("hooks"), dict)
    hooks = data["hooks"] if nested else data
    _claude_shaped(hooks, session_start_matcher=None)
    if nested:
        data["hooks"] = hooks
        _write_json(path, data)
    else:
        _write_json(path, hooks)
    return path


def update_codex_hooks() -> Path:
    path = home() / ".codex" / "hooks.json"
    root = _read_json(path)
    hooks = root.get("hooks") if isinstance(root.get("hooks"), dict) else {}
    merge_hook(hooks, "Stop", "on_stop", timeout=30)
    merge_hook(hooks, "PreToolUse", "on_pre_tool", matcher="*", timeout=PRE_TOOL_TIMEOUT)
    merge_hook(hooks, "PostToolUse", "on_post_tool", matcher="*", timeout=10)
    merge_hook(hooks, "UserPromptSubmit", "on_user_prompt", timeout=10)
    root["hooks"] = hooks
    _write_json(path, root)
    # Codex only runs hooks behind its feature flag.
    cfg = home() / ".codex" / "config.toml"
    text = cfg.read_text() if cfg.exists() else ""
    if "codex_hooks = true" in text:
        cfg.write_text(text.replace("codex_hooks = true", "hooks = true"))
    elif "hooks = true" not in text:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text + "\n[features]\nhooks = true\n")
    return path


def update_gemini_hooks() -> Path:
    path = home() / ".gemini" / "settings.json"
    settings = _read_json(path)
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    merge_hook(hooks, "BeforeTool", "on_pre_tool", matcher="*", timeout=PRE_TOOL_TIMEOUT)
    merge_hook(hooks, "AfterAgent", "on_stop", timeout=30)
    merge_hook(hooks, "SessionEnd", "on_stop", timeout=30)
    settings["hooks"] = hooks
    _write_json(path, settings)
    return path


def update_grok_hooks() -> Path:
    path = home() / ".grok" / "hooks" / "aaw-core.json"

    def entry(name: str, timeout: int, matcher: str | None = None) -> dict:
        e: dict = {"hooks": [{"type": "command", "command": hook_command(name), "timeout": timeout}],
                   "env": {"AAW_AGENT": "grok"}}
        if matcher:
            e["matcher"] = matcher
        return e

    _write_json(path, {"hooks": {
        "PreToolUse": [entry("on_pre_tool", PRE_TOOL_TIMEOUT, ".*")],
        "PostToolUse": [entry("on_post_tool", 10)],
        "Stop": [entry("on_stop", 30)],
        "SessionEnd": [entry("on_stop", 30)],
        "Notification": [entry("on_notification", 10)],
    }})
    return path


def update_cursor_hooks() -> Path:
    """Cursor's file is flat: one hook object per event entry (no groups). Another tool
    (the commercial host, for one) may own entries in it, so ours are merged: replaced
    where they already exist, appended otherwise."""
    path = home() / ".cursor" / "hooks.json"
    data = _read_json(path)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    wanted = {
        "preToolUse": ("on_pre_tool", PRE_TOOL_TIMEOUT), "beforeShellExecution": ("on_pre_tool", PRE_TOOL_TIMEOUT),
        "beforeMCPExecution": ("on_pre_tool", PRE_TOOL_TIMEOUT), "postToolUse": ("on_post_tool", 10),
        "stop": ("on_stop", 30), "sessionEnd": ("on_stop", 30), "notification": ("on_notification", 10),
    }
    for event, (name, timeout) in wanted.items():
        entries = [e for e in (hooks.get(event) or []) if MARKER not in json.dumps(e)]
        entries.append({"command": hook_command(name), "type": "command", "timeout": timeout})
        hooks[event] = entries
    data["version"] = data.get("version", 1)
    data["hooks"] = hooks
    _write_json(path, data)
    return path


def install_all(log=print) -> list[str]:
    """Register hooks for every agent found on this machine. Claude Code's file is always
    written (its hooks are harmless when Claude is absent); the others only when the binary
    is present. Returns the agents done."""
    done = []
    log(f"hooks: {update_claude_hooks()} (Claude Code)")
    done.append("claude")
    for agent, fn in (("scoot", update_scoot_hooks), ("codex", update_codex_hooks),
                      ("gemini", update_gemini_hooks), ("grok", update_grok_hooks), ("cursor", update_cursor_hooks)):
        if is_installed(agent):
            log(f"hooks: {fn()} ({agent})")
            done.append(agent)
    return done


def _strip_merged_hooks(path: Path) -> bool:
    """Remove only our hook groups from a shared config, leaving the user's own hooks and
    other keys untouched. True if it changed anything."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("hooks"), dict):
        hooks, nested = data["hooks"], True
    elif "hooks" not in data and data and all(isinstance(v, list) for v in data.values()):
        hooks, nested = data, False  # scoot flat: {event: [...]}
    else:
        return False
    changed = False
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if MARKER not in json.dumps(g)]
        if len(kept) != len(groups):
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not changed:
        return False
    if nested:
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
        remaining = data
        if set(remaining) <= {"version"}:  # Cursor's file with nothing left but its version tag
            remaining = {}
    else:
        remaining = hooks
    if remaining:
        _write_json(path, remaining)
    else:
        path.unlink(missing_ok=True)
    return True


def remove_all() -> list[str]:
    """Undo install_all(): strip our entries from the shared configs and delete the files we
    own outright. Codex's `[features] hooks = true` is left alone (other tools may rely on
    it). Returns the paths touched."""
    h = home()
    touched: list[str] = []
    for path in (h / ".claude" / "settings.json", h / ".codex" / "hooks.json",
                 h / ".gemini" / "settings.json", h / ".config" / "scoot" / "hooks.json",
                 h / ".cursor" / "hooks.json"):
        if _strip_merged_hooks(path):
            touched.append(str(path))
    grok = h / ".grok" / "hooks" / "aaw-core.json"
    try:
        ours = grok.exists() and MARKER in grok.read_text()
    except OSError:
        ours = False
    if ours:
        grok.unlink(missing_ok=True)
        touched.append(str(grok))
    return touched


# ── shell integration ─────────────────────────────────────────────────────────
# One line in the login shell's rc makes a plain `claude` (codex, gemini, grok,
# cursor-agent, scoot) in a folder start a bridged session. The functions come from
# `aaw shell-init` at shell start, so an upgrade or a moved install never leaves a
# stale path behind in the rc file.

SHELL_MARKER = "aaw shell-init"
SHELL_COMMENT = "# Agents At Work Core: a plain claude/codex/gemini/grok/cursor-agent/scoot starts a bridged session"
SHELL_LINE = 'command -v aaw >/dev/null 2>&1 && eval "$(aaw shell-init)"'


def shell_rc() -> Path:
    shell = os.path.basename(os.environ.get("SHELL", "")) or "bash"
    return home() / (".zshrc" if shell == "zsh" else ".bashrc")


def shell_integration_status() -> bool:
    rc = shell_rc()
    try:
        return rc.exists() and SHELL_MARKER in rc.read_text(errors="replace")
    except OSError:
        return False


def add_shell_integration() -> bool:
    """True when the line was added; False when it was already there."""
    rc = shell_rc()
    existing = rc.read_text(errors="replace") if rc.exists() else ""
    if SHELL_MARKER in existing:
        return False
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    rc.write_text(existing + sep + "\n" + SHELL_COMMENT + "\n" + SHELL_LINE + "\n")
    return True


def remove_shell_integration() -> bool:
    rc = shell_rc()
    if not rc.exists():
        return False
    lines = rc.read_text(errors="replace").splitlines(keepends=True)
    kept = [ln for ln in lines if SHELL_MARKER not in ln and ln.strip() != SHELL_COMMENT]
    if len(kept) == len(lines):
        return False
    rc.write_text("".join(kept))
    return True
