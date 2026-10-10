"""CLI argument parsing in ``src.main``.

The command line only carries what the web server needs (config path, host,
port, log level). Game setup - sides, profiles - happens in the browser, so the
old ``--mode`` / ``--white`` / ``--headless`` flags are gone.
"""

import pytest

from src.main import build_parser


def test_parser_defaults_are_server_only():
    args = build_parser().parse_args([])
    assert args.config.endswith("config.yaml")
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.log_level is None


def test_parser_accepts_overrides():
    args = build_parser().parse_args(
        ["--config", "x.yaml", "--host", "0.0.0.0", "--port", "9000", "--log-level", "DEBUG"]
    )
    assert args.config == "x.yaml"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.log_level == "DEBUG"


def test_parser_rejects_game_flags():
    # The game is configured in the UI now; launch-time game flags are removed.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "llm-vs-llm"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--headless"])
