"""Command line entry point: launch the web UI and backend.

The game itself is created from the browser - pick "New game" (choosing who
plays white/black and, for LLM sides, which profile from ``config.yaml``) or
"Load" an existing one. The command line only carries what the server needs:
where to find the config, the host/port, and the log level.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, load_config
from src.game.storage import StoragePaths, setup_logging
from src.ui.server import create_app

LOGGER = logging.getLogger("src.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chess-llm",
        description="Human vs LLM and LLM vs LLM chess over an OpenAI-compatible API.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to config.yaml (copied from config.example.yaml).",
    )
    parser.add_argument("--port", type=int, default=8000, help="web server port")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING or ERROR")
    return parser


BIND_HOST = "127.0.0.1"
"""The only address the web UI listens on.

There is no ``--host`` flag and no config key for it. The API has no
authentication and every LLM turn spends the operator's credits, so it is meant
to be reached from this machine and nothing else. Binding a public address and
rejecting requests afterwards would still leave the port visible to scanners,
and a flag to do so would only be a trap.
"""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg_path = Path(args.config)
    # A missing config is allowed at launch: the UI still loads, but starting an
    # LLM game then fails with a clear "profile not configured" message rather
    # than crashing here. A human vs human game needs no config at all. A config
    # that exists but is malformed is reported and the process stops.
    try:
        config = load_config(cfg_path, required=False)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return 2
    if args.log_level:
        config.storage.log_level = args.log_level

    paths = StoragePaths.from_config(config.storage)
    paths.ensure()
    # "server" is a placeholder id for the boot-time log file; each game resets
    # the logger to its own log_<timestamp>.log when it starts.
    setup_logging("server", paths, level=config.storage.log_level,
                  to_console=config.storage.log_to_console)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.error("The web UI needs fastapi and uvicorn. Run: pip install fastapi uvicorn")
        return 2

    app = create_app(config=config, paths=paths)
    print(f"chess-llm web UI: http://{BIND_HOST}:{args.port}")
    print("No game is running yet - create a new game or load a saved one in the browser.")
    uvicorn.run(app, host=BIND_HOST, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
