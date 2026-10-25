"""CLI argument parsing in ``src.main``.

The command line only carries what the web server needs (config path, port, log
level). Game setup - sides, profiles - happens in the browser, so the old
``--mode`` / ``--white`` / ``--headless`` flags are gone. ``--host`` went the
same way: the server only ever listens on loopback, so the address is not a
choice the operator should be making.
"""

import ipaddress

import pytest

from src.main import BIND_HOST, build_parser


def test_parser_defaults_are_server_only():
    args = build_parser().parse_args([])
    assert args.config.endswith("config.yaml")
    assert args.port == 8000
    assert args.log_level is None
    assert not hasattr(args, "host")


def test_parser_accepts_overrides():
    args = build_parser().parse_args(
        ["--config", "x.yaml", "--port", "9000", "--log-level", "DEBUG"]
    )
    assert args.config == "x.yaml"
    assert args.port == 9000
    assert args.log_level == "DEBUG"


def test_parser_rejects_removed_flags():
    # The game is configured in the UI now, and the bind address is fixed.
    for flag in (["--host", "0.0.0.0"], ["--mode", "llm-vs-llm"], ["--headless"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(flag)


def test_the_bind_address_is_loopback():
    """Pinned so a future change has to argue with this test first.

    The API has no authentication and every LLM turn spends the operator's
    credits. Binding a public address and rejecting requests afterwards still
    advertises the port to scanners, which is why there is no flag for it.
    """

    assert ipaddress.ip_address(BIND_HOST).is_loopback
