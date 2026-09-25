"""CLI argument resolution in ``src.main``."""

from __future__ import annotations

import pytest

from src.main import MODE_MAP, build_parser, resolve_sides


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("human-vs-llm", ("human", "llm")),
        ("llm-vs-human", ("llm", "human")),
        ("llm-vs-llm", ("llm", "llm")),
        ("human-vs-human", ("human", "human")),
    ],
)
def test_mode_shortcut_sets_both_sides(mode, expected):
    args = build_parser().parse_args(["--mode", mode])
    assert resolve_sides(args) == expected


def test_default_without_any_flag_is_human_vs_llm():
    args = build_parser().parse_args([])
    assert resolve_sides(args) == ("human", "llm")


def test_explicit_side_flag_wins_over_the_mode():
    args = build_parser().parse_args(["--mode", "human-vs-human", "--black", "llm"])
    assert resolve_sides(args) == ("human", "llm")


def test_every_documented_mode_is_accepted():
    args = build_parser().parse_args(["--mode", "llm-vs-llm"])
    assert args.mode in MODE_MAP
