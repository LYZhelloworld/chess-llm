"""CLI argument parsing in ``src.main``.

The command line only carries what the web server needs (config path, host,
port, log level). Game setup - sides, profiles - happens in the browser, so the
old ``--mode`` / ``--white`` / ``--headless`` flags are gone.
"""

import pytest

from src.main import bind_refusal, build_parser


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


def test_loopback_binds_are_always_allowed():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert bind_refusal(host, allow_remote=False) is None


def test_public_bind_is_refused_unless_remote_access_is_opted_in():
    for host in ("0.0.0.0", "192.168.1.10"):
        message = bind_refusal(host, allow_remote=False)
        assert message is not None
        assert host in message
        assert "allow_remote" in message

    assert bind_refusal("0.0.0.0", allow_remote=True) is None
