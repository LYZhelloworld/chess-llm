"""Command line entry point.

Examples:
    python -m src.main                                  # human (white) vs LLM (black)
    python -m src.main --mode llm-vs-llm               # LLM vs LLM, watch in the browser
    python -m src.main --mode llm-vs-llm --headless
    python -m src.main --mode human-vs-human            # local hot-seat, no LLM needed
    python -m src.main --load game_20260925_195646
"""

from __future__ import annotations

import argparse
import atexit
import logging
import random
import sys
from pathlib import Path
from typing import Optional, Tuple

import chess

from src.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    BLACK,
    HUMAN,
    LLM,
    WHITE,
    load_config,
    missing_config_message,
)
from src.game.controller import GameController
from src.game.state import GameState, PlayerSpec
from src.game.storage import StoragePaths, load_game, new_game_id, setup_logging

LOGGER = logging.getLogger("src.main")

# --mode is a shortcut for --white/--black; explicit side flags still win.
MODE_MAP = {
    "human-vs-llm": (HUMAN, LLM),
    "llm-vs-human": (LLM, HUMAN),
    "llm-vs-llm": (LLM, LLM),
    "human-vs-human": (HUMAN, HUMAN),
}


def resolve_sides(args: argparse.Namespace) -> Tuple[str, str]:
    """Return ``(white_kind, black_kind)`` from --mode and/or --white/--black."""

    white, black = args.white, args.black
    if args.mode:
        mode_white, mode_black = MODE_MAP[args.mode]
        white = white or mode_white
        black = black or mode_black
    return white or HUMAN, black or LLM


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chess-llm",
        description="Human vs LLM and LLM vs LLM chess over an OpenAI-compatible API.",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_MAP),
        default=None,
        help="shortcut for the two sides: " + ", ".join(sorted(MODE_MAP)),
    )
    parser.add_argument("--white", choices=[HUMAN, LLM], default=None, help="who plays white")
    parser.add_argument("--black", choices=[HUMAN, LLM], default=None, help="who plays black")
    parser.add_argument("--white-profile", default=None, help="LLM profile name for white")
    parser.add_argument("--black-profile", default=None, help="LLM profile name for black")
    parser.add_argument("--model", default=None, help="model override for both LLM sides")
    parser.add_argument("--white-model", default=None, help="model override for white")
    parser.add_argument("--black-model", default=None, help="model override for black")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL override")
    parser.add_argument("--api-key", default=None, help="API key override for both LLM sides")
    parser.add_argument("--api-key-env", default=None, help="environment variable holding the API key")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.yaml")
    parser.add_argument("--load", default=None, help="game id to resume, e.g. game_20260925_195646")
    parser.add_argument("--seed", type=int, default=None, help="seed for random fallback moves")
    parser.add_argument("--headless", action="store_true", help="run without the web UI")
    parser.add_argument("--host", default="127.0.0.1", help="web server host")
    parser.add_argument("--port", type=int, default=8000, help="web server port")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING or ERROR")
    return parser


def _make_spec(side: str, kind: str, args: argparse.Namespace, config: AppConfig) -> PlayerSpec:
    if kind == HUMAN:
        return PlayerSpec.human()

    profile_name = getattr(args, f"{side}_profile") or config.default_profile
    profile = config.profile(profile_name)
    model_override = getattr(args, f"{side}_model") or args.model
    overrides = {
        "model": model_override,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "api_key_env": args.api_key_env,
    }
    if any(value for value in overrides.values()):
        profile = profile.override(**{k: v for k, v in overrides.items() if v})
        profile_name = f"{side}_cli"
        config.llm_profiles[profile_name] = profile
    return PlayerSpec.llm(profile=profile_name, model=profile.model)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    white_kind, black_kind = resolve_sides(args)

    # A config file is mandatory as soon as one side is an LLM. A pure
    # human vs human game runs without it (useful when no endpoint exists yet).
    cfg_path = Path(args.config)
    config = load_config(cfg_path, required=False)
    config_missing = not cfg_path.exists()
    if args.seed is not None:
        config.seed = args.seed
    if args.log_level:
        config.storage.log_level = args.log_level

    paths = StoragePaths.from_config(config.storage)
    paths.ensure()

    if args.load:
        game_id = args.load
        if game_id.endswith(".json"):
            game_id = game_id[: -len(".json")]
        state = load_game(game_id, paths)
        LOGGER.debug("Loaded game %s", game_id)
    else:
        state = GameState(
            game_id=new_game_id(),
            start_fen=chess.STARTING_FEN,
            white=_make_spec(WHITE, white_kind, args, config),
            black=_make_spec(BLACK, black_kind, args, config),
            seed=config.seed,
        )

    if config_missing:
        if state.white.is_llm or state.black.is_llm:
            print(f"error: {missing_config_message(cfg_path)}", file=sys.stderr)
            return 2
        print(
            "notice: config.yaml not found - running without LLM profiles "
            "(human vs human). Create it to play against a model.",
            file=sys.stderr,
        )

    logger, log_path = setup_logging(
        state.game_id,
        paths,
        level=config.storage.log_level,
        to_console=config.storage.log_to_console,
    )
    logger.info(
        "Starting %s (white=%s, black=%s)",
        state.game_id,
        state.white.display_name,
        state.black.display_name,
    )

    controller = GameController(
        state, config, paths, rng=random.Random(state.seed)
    )
    atexit.register(controller.close)

    if args.headless:
        return _run_headless(controller, log_path)

    try:
        import uvicorn

        from src.ui.server import create_app
    except ImportError:  # pragma: no cover - optional dependency
        LOGGER.error("The web UI needs fastapi and uvicorn. Run: pip install fastapi uvicorn")
        return 2

    app = create_app(controller, config, paths)
    print(f"chess-llm web UI: http://{args.host}:{args.port}  (game {state.game_id})")
    print(f"Log file: {log_path}")
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    controller.close()
    return 0


def _run_headless(controller: GameController, log_path) -> int:
    if not (controller.state.white.is_llm and controller.state.black.is_llm):
        LOGGER.error(
            "Headless mode requires both sides to be LLMs (try --mode llm-vs-llm); "
            "a human side needs the web UI."
        )
        return 2

    print(f"Headless game {controller.state.game_id} - log: {log_path}")
    guard = 0
    while not controller.finished:
        guard += 1
        if guard > 1000:
            LOGGER.error("Safety guard triggered; stopping the headless loop.")
            break
        outcome = controller.step()
        status = outcome.get("status")
        if status == "waiting_human":
            break
        if status == "error":
            LOGGER.error("Stopping: %s", outcome.get("message"))
            break
        if status == "moved":
            print(
                f"{outcome['side']:>5} played {outcome['move']}"
                + (" (random fallback)" if outcome.get("fallback_random") else "")
            )
            print(controller.engine.ascii)
    result = controller.state.result or {}
    print(f"Result: {result.get('pgn_result', '*')} - {result.get('reason', 'unfinished')}")
    controller.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
