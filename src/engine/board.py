"""Thin wrapper around ``python-chess`` used by every other layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import chess

from src.engine.errors import MoveError, classify_exception


@dataclass
class MoveRecord:
    """A move that has been applied to a board."""

    san: str
    uci: str
    side: str


class ChessEngine:
    """Owns the authoritative board and exposes small, well typed helpers."""

    def __init__(self, fen: str = chess.STARTING_FEN) -> None:
        self.board = chess.Board(fen)
        self.last_move: Optional[MoveRecord] = None

    # ------------------------------------------------------------------ state
    @property
    def fen(self) -> str:
        return self.board.fen()

    @property
    def ascii(self) -> str:
        return str(self.board)

    @property
    def unicode(self) -> str:
        return self.board.unicode(invert_color=True, empty_square=".")

    @property
    def side_to_move(self) -> str:
        return "white" if self.board.turn == chess.WHITE else "black"

    @property
    def is_check(self) -> bool:
        return self.board.is_check()

    @property
    def castling_rights(self) -> str:
        return self.board.castling_xfen() if self.board.castling_rights else "-"

    @property
    def en_passant(self) -> str:
        ep = self.board.ep_square
        return chess.square_name(ep) if ep is not None else "-"

    @property
    def halfmove_clock(self) -> int:
        return self.board.halfmove_clock

    @property
    def fullmove_number(self) -> int:
        return self.board.fullmove_number

    @property
    def ply(self) -> int:
        return len(self.board.move_stack)

    @property
    def last_move_san(self) -> Optional[str]:
        """SAN of the last played move.

        python-chess can only render the SAN of a move *before* it is pushed, so
        the value is captured while the move is played.
        """

        return self.last_move.san if self.last_move else None

    def check_status(self) -> str:
        if not self.board.is_check():
            return "no check"
        return "white is in check" if self.board.turn == chess.WHITE else "black is in check"

    def san_of(self, move: chess.Move) -> str:
        return self.board.san(move)

    # ------------------------------------------------------------------ moves
    def legal_moves(self) -> List[Tuple[str, str]]:
        """Return ``(san, uci)`` pairs for every legal move."""

        return [(self.board.san(m), m.uci()) for m in self.board.legal_moves]

    def legal_moves_san(self) -> List[str]:
        return [san for san, _ in self.legal_moves()]

    def uci_for_san(self, san: str) -> str:
        move = self.parse_san(san)
        return move.uci()

    def parse_san(self, san: str) -> chess.Move:
        if not isinstance(san, str) or not san.strip():
            raise MoveError(
                "invalid_input",
                "Invalid input: the move must be a non-empty string in SAN format.",
            )
        try:
            return self.board.parse_san(san.strip())
        except Exception as exc:  # noqa: BLE001 - reclassified below
            raise classify_exception(exc, {"san": san}) from exc

    def parse_uci(self, uci: str) -> chess.Move:
        try:
            move = chess.Move.from_uci(uci)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc, {"uci": uci}) from exc
        if move not in self.board.legal_moves:
            raise MoveError(
                "illegal_move",
                f"Illegal move: {uci} is not legal in the current position.",
            )
        return move

    def apply_san(self, san: str) -> MoveRecord:
        move = self.parse_san(san)
        return self._push(move)

    def apply_uci(self, uci: str) -> MoveRecord:
        move = self.parse_uci(uci)
        return self._push(move)

    def _push(self, move: chess.Move) -> MoveRecord:
        side = "white" if self.board.turn == chess.WHITE else "black"
        san = self.board.san(move)
        try:
            self.board.push(move)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc, {"uci": move.uci()}) from exc
        self.last_move = MoveRecord(san=san, uci=move.uci(), side=side)
        return self.last_move

    # ------------------------------------------------------------- simulation
    def snapshot(self) -> "ChessEngine":
        return ChessEngine(self.board.fen())

    def board_from_fen(self, fen: Optional[str]) -> chess.Board:
        if fen is None or (isinstance(fen, str) and not fen.strip()):
            return self.board.copy()
        if not isinstance(fen, str):
            raise MoveError("invalid_input", "Invalid input: FEN must be a string.")
        try:
            return chess.Board(fen.strip())
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc, {"fen": fen}) from exc

    def simulate(self, fen: Optional[str], moves: List[str]) -> Dict[str, Any]:
        """Apply ``moves`` to ``fen`` (or the live board) without mutating it."""

        board = self.board_from_fen(fen)
        steps: List[Dict[str, Any]] = []
        applied = 0
        error: Optional[MoveError] = None

        for index, raw in enumerate(moves):
            if not isinstance(raw, str) or not raw.strip():
                error = MoveError(
                    "invalid_input",
                    f"Invalid input: move #{index + 1} must be a non-empty SAN string.",
                    detail={"index": index, "value": raw},
                )
                steps.append({"index": index, "san": raw, "ok": False, "error": error.to_dict()})
                break
            san = raw.strip()
            try:
                move = board.parse_san(san)
            except Exception as exc:  # noqa: BLE001
                error = classify_exception(exc, {"index": index, "san": san})
                steps.append({"index": index, "san": san, "ok": False, "error": error.to_dict()})
                break
            side = "white" if board.turn == chess.WHITE else "black"
            board.push(move)
            applied += 1
            steps.append(
                {
                    "index": index,
                    "san": san,
                    "uci": move.uci(),
                    "side": side,
                    "ok": True,
                }
            )

        return {
            "board": board,
            "steps": steps,
            "applied": applied,
            "error": error,
            "ok": error is None and applied == len(moves),
        }

    # ------------------------------------------------------------------ misc
    def reset(self, fen: str = chess.STARTING_FEN) -> None:
        self.board = chess.Board(fen)
        self.last_move = None
