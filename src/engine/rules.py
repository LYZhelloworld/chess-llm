"""Terminal state detection: checkmate, stalemate and every draw flavour."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import chess

from src.config import DrawConfig


@dataclass
class GameResult:
    """Outcome of a finished game."""

    winner: Optional[str]  # "white", "black" or None for a draw
    reason: str
    detail: str = ""
    pgn_result: str = "*"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["GameResult"]:
        if not data:
            return None
        return cls(
            winner=data.get("winner"),
            reason=data.get("reason", ""),
            detail=data.get("detail", ""),
            pgn_result=data.get("pgn_result", "*"),
        )


def evaluate_position(board: chess.Board, draw: DrawConfig) -> Optional[GameResult]:
    """Return a :class:`GameResult` when the position is terminal, else ``None``."""

    if board.is_checkmate():
        winner = "black" if board.turn == chess.WHITE else "white"
        return GameResult(
            winner=winner,
            reason="checkmate",
            detail=f"{winner.capitalize()} delivered checkmate.",
            pgn_result="1-0" if winner == "white" else "0-1",
        )

    if draw.stalemate and board.is_stalemate():
        return GameResult(
            winner=None,
            reason="stalemate",
            detail="No legal move is available and the side to move is not in check.",
            pgn_result="1/2-1/2",
        )

    if draw.insufficient_material and board.is_insufficient_material():
        return GameResult(
            winner=None,
            reason="insufficient_material",
            detail="Neither side has enough material to deliver checkmate.",
            pgn_result="1/2-1/2",
        )

    if board.is_repetition(draw.repetition_count):
        return GameResult(
            winner=None,
            reason="repetition",
            detail=f"The same position occurred {draw.repetition_count} times.",
            pgn_result="1/2-1/2",
        )

    if board.halfmove_clock >= 2 * draw.no_progress_moves:
        return GameResult(
            winner=None,
            reason="no_progress",
            detail=(
                f"{draw.no_progress_moves} full moves without a capture or a pawn move "
                f"(halfmove clock {board.halfmove_clock})."
            ),
            pgn_result="1/2-1/2",
        )

    full_moves = board.fullmove_number
    if full_moves > draw.max_game_moves:
        return GameResult(
            winner=None,
            reason="move_limit",
            detail=f"Reached the configured limit of {draw.max_game_moves} full moves.",
            pgn_result="1/2-1/2",
        )

    return None


def resignation_result(side: str, reason: Optional[str] = None) -> GameResult:
    winner = "black" if side == "white" else "white"
    detail = reason or f"{side.capitalize()} resigned."
    return GameResult(
        winner=winner,
        reason="resignation",
        detail=detail,
        pgn_result="1-0" if winner == "white" else "0-1",
    )
