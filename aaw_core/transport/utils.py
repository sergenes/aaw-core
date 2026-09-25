"""Shared text utilities for the transport layer."""

from __future__ import annotations

_BOX_HORIZONTAL = set("─━═")
_BOX_VERTICAL = set("│║┃")
_BOX_CORNERS = set("┌┐└┘╔╗╚╝╭╮╰╯")
_BOX_CROSS = set("├┤┬┴┼╠╣╦╩╬╪╫")
_BOX_ALL = _BOX_HORIZONTAL | _BOX_VERTICAL | _BOX_CORNERS | _BOX_CROSS


def _is_border_only(line: str) -> bool:
    """True if the line is only box-drawing characters and spaces (no text)."""
    stripped = line.strip()
    return bool(stripped) and all(c in _BOX_ALL or c == " " for c in stripped)


def _convert_separator(line: str) -> str:
    """Turn a mid-table separator (├───┼───┤) into a markdown separator (|---|---|)."""
    out = []
    for c in line:
        if c in _BOX_HORIZONTAL:
            out.append("-")
        elif c in (_BOX_CROSS | _BOX_CORNERS | _BOX_VERTICAL):
            out.append("|")
        else:
            out.append(c)
    return "".join(out).rstrip()


def clean_box_tables(text: str) -> str:
    """Convert Unicode box-drawing tables in ``text`` to plain markdown-style tables.

    Top/bottom borders (corners, no text) are dropped; mid-table separators
    (cross characters, no text) become ``|---|---|``; content rows get their
    vertical bars replaced with ``|``. Agents render tables this way in the
    terminal, and the phone renders markdown.
    """
    out = []
    for line in text.split("\n"):
        if _is_border_only(line):
            has_corner = any(c in _BOX_CORNERS for c in line)
            has_cross = any(c in _BOX_CROSS for c in line)
            if has_corner and not has_cross:
                continue  # pure top/bottom border
            out.append(_convert_separator(line))
        elif any(c in _BOX_VERTICAL for c in line):
            out.append(line.replace("│", "|").replace("║", "|").replace("┃", "|").rstrip())
        else:
            out.append(line)
    return "\n".join(out)
