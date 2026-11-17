"""A dialog in a small terminal is read with room (tmux.dialog_pane).

Seen 2026-11-16 with a window tiler that keeps unfocused agents in tiles of about 80x8:
Claude Code folded the `/model` menu to one option between "↑ 1 more" and "↓ 4 more",
the detector found no menu on the screen, and the phone got no card until the tile grew."""

from __future__ import annotations

import subprocess

import pytest

from aaw_core.host import detect, tmux

FOLDED = """    ↓ 4 more

  ◐ Medium effort (default) ←/→ to adjust

  Use /fast to turn on fast mode (Opus 5.5).

  Enter to set as default · s to use this session only · Esc to cancel
"""
FULL = """❯ /model

────────────────────────────────────────────────────────────────────────
  Select model
  Switch between Claude models. Your pick becomes the default for new sessions.

    1.  Default (recommended)  Opus 5.5 · Best for everyday, complex tasks
  ❯ 2.  Opus 5.5 ✔             For complex work and everyday tasks
    3.  Fable 5.2              For your toughest challenges
    4.  Sonnet 5.5             Most efficient for simpler tasks
    5.  Haiku 5.5              Fastest for quick answers
    ▸   More models

  ◐ Medium effort (default) ←/→ to adjust

  Use /fast to turn on fast mode (Opus 5.5).

  Enter to set as default · s to use this session only · Esc to cancel
"""
IDLE = "❯ \n  ? for shortcuts\n"


class FakeTmux:
    """A session whose agent draws `folded` in its tile and `full` when given room."""

    def __init__(self, cols=80, rows=8, folded=FOLDED, full=FULL):
        self.tile = (cols, rows)
        self.size = (cols, rows)
        self.folded, self.full = folded, full
        self.calls: list[list] = []
        self.manual = False

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        out = ""
        if args[0] == "display-message":
            out = f"{self.size[0]} {self.size[1]}\n"
        elif args[0] == "capture-pane":
            out = self.full if self.size[1] >= 20 else self.folded
        elif args[0] == "resize-window" and "-A" in args:
            self.size = self.tile
        elif args[0] == "resize-window":
            self.size = (int(args[args.index("-x") + 1]), int(args[args.index("-y") + 1]))
            self.manual = True
        elif args[0] == "set-window-option" and "-u" in args:
            self.manual = False
        return subprocess.CompletedProcess(args, 0, out, "")

    def resizes(self) -> int:
        return sum(1 for c in self.calls if c[0] == "resize-window" and "-A" not in c)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(tmux, "REDRAW_S", 0)
    tmux._readings.clear()

    def install(**kwargs) -> FakeTmux:
        f = FakeTmux(**kwargs)
        monkeypatch.setattr(tmux, "tmux_run", f)
        return f
    return install


def test_the_folded_screen_alone_shows_no_menu():
    assert detect.detect_permission_prompt(FOLDED) is None
    assert detect.detect_permission_prompt(FULL)["question"] == "Select model"


def test_a_folded_dialog_is_read_with_room_and_the_tile_gets_its_size_back(fake):
    t = fake()
    menu = detect.detect_permission_prompt(tmux.dialog_pane("aaw-p"))
    assert menu["question"] == "Select model" and len(menu["options"]) == 5 and menu["menu"]
    assert ["resize-window", "-t", "aaw-p", "-x", "120", "-y", "40"] in t.calls
    # back to the terminal's size, and following it again (not left at window-size manual)
    assert t.size == (80, 8) and t.manual is False


def test_the_window_is_resized_once_per_dialog_not_on_every_poll(fake):
    t = fake()
    for _ in range(5):
        assert "Select model" in tmux.dialog_pane("aaw-p")
    assert t.resizes() == 1


def test_a_changed_dialog_is_read_again_but_not_more_often_than_the_limit(fake, monkeypatch):
    t = fake()
    tmux.dialog_pane("aaw-p")
    t.folded = FOLDED.replace("↓ 4 more", "↓ 3 more")  # the cursor moved
    assert "Select model" in tmux.dialog_pane("aaw-p") and t.resizes() == 1  # too soon: the last reading
    monkeypatch.setattr(tmux, "GROW_EVERY_S", 0)
    tmux.dialog_pane("aaw-p")
    assert t.resizes() == 2


@pytest.mark.parametrize("kwargs", [
    {"cols": 120, "rows": 34},          # a terminal with room: read as it is
    {"folded": IDLE, "full": IDLE},     # a small tile with no dialog: left alone
])
def test_nothing_is_resized_without_a_folded_dialog(fake, kwargs):
    t = fake(**kwargs)
    tmux.dialog_pane("aaw-p")
    assert t.resizes() == 0


def test_the_reading_is_dropped_when_the_dialog_closes(fake):
    t = fake()
    tmux.dialog_pane("aaw-p")
    t.folded = IDLE
    assert tmux.dialog_pane("aaw-p") == IDLE
    t.folded = FOLDED  # the same menu opened again: a new dialog, a new reading
    tmux.dialog_pane("aaw-p")
    assert t.resizes() == 2


def test_a_resize_that_tmux_refuses_falls_back_to_the_screen(fake, monkeypatch):
    t = fake()
    plain = t.__call__

    def refusing(args, **kwargs):
        if args[0] == "resize-window" and "-A" not in args:
            t.calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, "", "no")
        return plain(args, **kwargs)
    monkeypatch.setattr(tmux, "tmux_run", refusing)
    assert tmux.dialog_pane("aaw-p") == FOLDED
