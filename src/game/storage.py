"""Persistence: PGN, game JSON and timestamped logs."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import chess
import chess.pgn

from src.config import PROJECT_ROOT, StorageConfig
from src.game.state import GameState

LOGGER = logging.getLogger("src.game.storage")

GAME_ID_RE = re.compile(r"^game_(\d{8}_\d{6})$")


def new_game_id(now: Optional[datetime] = None) -> str:
    """``game_yyyyMMdd_HHmmss`` - also the base name of every artifact."""

    return "game_" + (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def timestamp_of(game_id: str) -> str:
    """Extract ``yyyyMMdd_HHmmss`` from a game id, falling back to now."""

    match = GAME_ID_RE.match(game_id or "")
    if match:
        return match.group(1)
    cleaned = re.sub(r"[^0-9_]", "", game_id or "")
    if re.match(r"^\d{8}_\d{6}$", cleaned):
        return cleaned
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class StoragePaths:
    games_dir: Path = PROJECT_ROOT / "games"
    logs_dir: Path = PROJECT_ROOT / "logs"

    @classmethod
    def from_config(cls, storage: StorageConfig, root: Path = PROJECT_ROOT) -> "StoragePaths":
        games = Path(storage.games_dir)
        logs = Path(storage.logs_dir)
        return cls(
            games_dir=games if games.is_absolute() else root / games,
            logs_dir=logs if logs.is_absolute() else root / logs,
        )

    def ensure(self) -> "StoragePaths":
        self.games_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self

    def json_path(self, game_id: str) -> Path:
        return self.games_dir / f"{game_id}.json"

    def pgn_path(self, game_id: str) -> Path:
        return self.games_dir / f"{game_id}.pgn"

    def log_path(self, game_id: str) -> Path:
        return self.logs_dir / f"log_{timestamp_of(game_id)}.log"


# --------------------------------------------------------------------------- io
def save_game(state: GameState, paths: StoragePaths) -> Tuple[Path, Path]:
    """Write the JSON record and the PGN snapshot. Both are rewritten in full."""

    paths.ensure()
    json_path = paths.json_path(state.game_id)
    pgn_path = paths.pgn_path(state.game_id)

    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(state.to_dict(), fh, ensure_ascii=False, indent=2)
    with pgn_path.open("w", encoding="utf-8") as fh:
        fh.write(build_pgn(state))
    return json_path, pgn_path


def load_game(game_id: str, paths: StoragePaths) -> GameState:
    """Rebuild a :class:`GameState` from ``games/<game_id>.json``."""

    path = paths.json_path(game_id)
    if not path.exists():
        raise FileNotFoundError(f"No saved game named '{game_id}' (expected {path})")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    state = GameState.from_dict(data)
    state.game_id = game_id
    return state


def list_games(paths: StoragePaths) -> List[str]:
    if not paths.games_dir.exists():
        return []
    return sorted(p.stem for p in paths.games_dir.glob("game_*.json"))


def _pgn_date(game_id: str) -> str:
    stamp = timestamp_of(game_id)
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").strftime("%Y.%m.%d")
    except ValueError:  # pragma: no cover - defensive
        return stamp.split("_")[0]


def build_pgn(state: GameState) -> str:
    """Render the current position history as a PGN document."""

    board = chess.Board(state.start_fen)
    game = chess.pgn.Game()
    if state.start_fen != chess.STARTING_FEN:
        game.setup(board)

    game.headers["Event"] = "chess-llm game"
    game.headers["Site"] = "chess-llm"
    game.headers["Date"] = _pgn_date(state.game_id)
    game.headers["Round"] = "1"
    game.headers["White"] = state.white.display_name
    game.headers["Black"] = state.black.display_name
    game.headers["WhiteKind"] = state.white.kind
    game.headers["BlackKind"] = state.black.kind
    if state.white.model:
        game.headers["WhiteModel"] = state.white.model
    if state.black.model:
        game.headers["BlackModel"] = state.black.model

    node: chess.pgn.GameNode = game
    for ply, san in enumerate(state.moves_san):
        try:
            move = board.parse_san(san)
        except Exception:  # noqa: BLE001 - keep the PGN usable if data is odd
            LOGGER.warning("Skipping unparsable move %r at ply %d", san, ply)
            break
        node = node.add_variation(move)
        board.push(move)
        comment = state.comment_for_ply(ply)
        if comment:
            node.comment = comment

    result = state.result or {}
    game.headers["Result"] = result.get("pgn_result", "*")
    if result.get("reason"):
        game.headers["Termination"] = f"{result.get('reason')}: {result.get('detail', '')}".strip(": ")
    game.headers["PlyCount"] = str(len(state.moves_san))

    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    return game.accept(exporter)


# ------------------------------------------------------------------------ logs
class MultilineFormatter(logging.Formatter):
    """A formatter that repeats the timestamp prefix on every single line."""

    def __init__(self, datefmt: str = "%Y-%m-%d %H:%M:%S", name_width: int = 24) -> None:
        super().__init__(datefmt=datefmt)
        self.name_width = name_width

    def _prefix(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, self.datefmt)
        level = record.levelname.ljust(5)
        name = (record.name or "root").ljust(self.name_width)
        return f"{stamp} | {level} | {name} | "

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            if not message.endswith("\n"):
                message += "\n"
            message += self.formatException(record.exc_info)
        if record.stack_info:
            if not message.endswith("\n"):
                message += "\n"
            message += self.formatStack(record.stack_info)
        lines = message.splitlines() or [""]
        prefix = self._prefix(record)
        return "\n".join(prefix + line for line in lines)


def setup_logging(
    game_id: str,
    paths: StoragePaths,
    level: str = "INFO",
    to_console: bool = True,
) -> Tuple[logging.Logger, Path]:
    """Configure the root logger to write ``logs/log_<timestamp>.log``."""

    paths.ensure()
    log_path = paths.log_path(game_id)
    formatter = MultilineFormatter()
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_GameLogFilter())
    root.addHandler(file_handler)

    if to_console:
        console = logging.StreamHandler()
        console.setLevel(numeric_level)
        console.setFormatter(formatter)
        console.addFilter(_GameLogFilter())
        root.addHandler(console)

    for noisy in ("openai", "httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("src.game")
    logger.info("Logging initialised for %s -> %s", game_id, log_path)
    return logger, log_path


class _GameLogFilter(logging.Filter):
    """Keep third-party chatter (httpx, uvicorn access logs) out of the game log."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        if name.startswith("uvicorn.error") and "access" in str(record.getMessage()):
            return False
        return not name.startswith(("httpx", "httpcore", "openai._base_client"))
