"""The session id rules: one folder + one agent = one id, never another folder's id."""

from __future__ import annotations

from aaw_core.host.session_id import Known, resolve, sanitize


def test_fresh_folder_gets_its_basename():
    r = resolve("/home/me/proj", "claude", [])
    assert (r.action, r.id, r.alongside) == ("launch", "proj", ())


def test_same_folder_same_agent_live_attaches():
    known = [Known("proj", "/home/me/proj", "claude", True)]
    r = resolve("/home/me/proj", "claude", known)
    assert (r.action, r.id) == ("attach", "proj")


def test_stopped_doc_is_reused_on_restart():
    known = [Known("proj", "/home/me/proj", "claude", False)]
    r = resolve("/home/me/proj", "claude", known)
    assert (r.action, r.id) == ("launch", "proj")


def test_another_agent_live_on_the_folder_gets_the_agent_suffix():
    known = [Known("proj", "/home/me/proj", "claude", True)]
    r = resolve("/home/me/proj", "codex", known)
    assert (r.action, r.id) == ("launch", "proj-codex")
    assert [k.agent for k in r.alongside] == ["claude"]


def test_basename_owned_by_another_folder_gets_the_parent_suffix():
    known = [Known("proj", "/other/proj", "claude", True)]
    r = resolve("/home/me/proj", "claude", known)
    assert r.id == "proj-me"


def test_numeric_fallback_when_parent_form_is_taken_too():
    known = [Known("proj", "/other/proj", "claude", True), Known("proj-me", "/third/me/proj", "claude", False)]
    r = resolve("/home/me/proj", "claude", known)
    assert r.id == "proj-me-2"


def test_never_an_id_owned_by_a_different_path_even_with_matching_agent():
    known = [Known("proj", "/other/proj", "codex", False), Known("proj-codex", "/x/proj", "codex", True),
             Known("proj-live", "/home/me/proj", "claude", True)]
    r = resolve("/home/me/proj", "codex", known)
    assert r.id not in ("proj", "proj-codex")
    assert r.id.startswith("proj-me")


def test_sanitize_keeps_tmux_safe_characters():
    assert sanitize("my.app:v2") == "my-app-v2"
    assert sanitize("..") == ""
    assert resolve("/home/me/my.app", "claude", []).id == "my-app"
