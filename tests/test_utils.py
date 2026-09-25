"""clean_box_tables: terminal box-drawing tables become markdown tables."""

from __future__ import annotations

from aaw_core.transport.utils import clean_box_tables

TABLE = (
    "┌──────┬─────┐\n"
    "│ name │ age │\n"
    "├──────┼─────┤\n"
    "│ ann  │ 41  │\n"
    "└──────┴─────┘"
)

# A border with T-junctions (┬ ┴) counts as a separator row; only corner-only borders are dropped.
EXPECTED = (
    "|------|-----|\n"
    "| name | age |\n"
    "|------|-----|\n"
    "| ann  | 41  |\n"
    "|------|-----|"
)


def test_box_table_becomes_markdown():
    assert clean_box_tables(TABLE) == EXPECTED


def test_corner_only_borders_are_dropped():
    assert clean_box_tables("╭────╮\n│ hi │\n╰────╯") == "| hi |"


def test_prose_and_double_lines():
    assert clean_box_tables("plain prose\nsecond line") == "plain prose\nsecond line"
    assert clean_box_tables("║ a ║ b ║") == "| a | b |"
    assert clean_box_tables("") == ""
