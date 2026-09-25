"""Dispatch tool calls coming from an LLM and always answer with JSON."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from src.engine.board import ChessEngine
from src.engine.errors import (
    CATEGORY_ILLEGAL_MOVE,
    CATEGORY_ILLEGAL_STATE,
    CATEGORY_INVALID_INPUT,
    CATEGORY_UNKNOWN_TOOL,
    MoveError,
    classify_exception,
)
from src.engine.rules import evaluate_position
from src.config import DrawConfig

LOGGER = logging.getLogger("src.tools.executor")


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class ToolExecutor:
    """Executes one tool call against the live engine.

    The executor is created for a single agent turn. It records whether the
    agent committed a move (``committed_move``) or resigned (``resigned``) so
    the agent loop knows when the turn is over.
    """

    def __init__(
        self,
        engine: ChessEngine,
        side: str,
        draw: Optional[DrawConfig] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.engine = engine
        self.side = side
        self.draw = draw or DrawConfig()
        self.on_event = on_event or (lambda _event: None)
        self.committed_move: Optional[str] = None
        self.committed_uci: Optional[str] = None
        self.resigned: bool = False
        self.resign_reason: Optional[str] = None
        self.terminal: bool = False

    # ------------------------------------------------------------------ public
    def execute(self, name: str, raw_arguments: str) -> str:
        """Run one tool call and return its JSON payload as a string."""

        try:
            arguments = self._parse_arguments(raw_arguments)
        except MoveError as err:
            return self._error(name, err)

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return self._error(
                name,
                MoveError(
                    CATEGORY_UNKNOWN_TOOL,
                    f"Unknown tool '{name}'. Available tools: "
                    + ", ".join(
                        [
                            "get_board",
                            "get_board_ascii",
                            "get_legal_moves",
                            "try_make_move",
                            "make_move",
                            "resign",
                        ]
                    ),
                ),
            )

        if self.terminal and name not in {"get_board", "get_board_ascii", "get_legal_moves"}:
            return _json(
                {
                    "ok": False,
                    "tool": name,
                    "error": {
                        "category": CATEGORY_ILLEGAL_STATE,
                        "message": (
                            "Your turn is already finished: "
                            + (
                                f"you played {self.committed_move}."
                                if self.committed_move
                                else "you resigned."
                            )
                        ),
                    },
                }
            )

        try:
            return handler(arguments)
        except MoveError as err:
            return self._error(name, err)
        except Exception as exc:  # noqa: BLE001 - never crash the agent loop
            LOGGER.exception("Unhandled error while running tool %s", name)
            return self._error(name, classify_exception(exc, {"tool": name}))

    # ------------------------------------------------------------------- tools
    def _tool_get_board(self, _args: Dict[str, Any]) -> str:
        board = self.engine.board
        result = evaluate_position(board, self.draw)
        return _json(
            {
                "ok": True,
                "tool": "get_board",
                "fen": self.engine.fen,
                "side_to_move": self.engine.side_to_move,
                "your_side": self.side,
                "check_status": self.engine.check_status(),
                "castling_rights": self.engine.castling_rights,
                "en_passant": self.engine.en_passant,
                "halfmove_clock": self.engine.halfmove_clock,
                "fullmove_number": self.engine.fullmove_number,
                "last_move": self.engine.last_move_san,
                "legal_moves_count": board.legal_moves.count(),
                "game_over": result is not None,
                "result": result.to_dict() if result else None,
            }
        )

    def _tool_get_board_ascii(self, _args: Dict[str, Any]) -> str:
        return _json(
            {
                "ok": True,
                "tool": "get_board_ascii",
                "fen": self.engine.fen,
                "side_to_move": self.engine.side_to_move,
                "ascii": self.engine.ascii,
            }
        )

    def _tool_get_legal_moves(self, _args: Dict[str, Any]) -> str:
        moves = self.engine.legal_moves_san()
        return _json(
            {
                "ok": True,
                "tool": "get_legal_moves",
                "fen": self.engine.fen,
                "side_to_move": self.engine.side_to_move,
                "count": len(moves),
                "moves": moves,
                "note": "make_move only accepts one of these exact SAN strings.",
            }
        )

    def _tool_try_make_move(self, args: Dict[str, Any]) -> str:
        moves = args.get("moves")
        if moves is None:
            raise MoveError(
                CATEGORY_INVALID_INPUT,
                "Invalid input: 'moves' is required and must be a list of SAN moves.",
            )
        if isinstance(moves, str):
            moves = [moves]
        if not isinstance(moves, list) or not moves:
            raise MoveError(
                CATEGORY_INVALID_INPUT,
                "Invalid input: 'moves' must be a non-empty list of SAN strings.",
            )

        fen = args.get("fen")
        outcome = self.engine.simulate(fen, moves)
        board = outcome["board"]
        steps: List[Dict[str, Any]] = outcome["steps"]
        error: Optional[MoveError] = outcome["error"]
        applied: int = outcome["applied"]

        payload: Dict[str, Any] = {
            "ok": outcome["ok"],
            "tool": "try_make_move",
            "simulated": True,
            "committed": False,
            "start_fen": fen.strip() if isinstance(fen, str) and fen.strip() else self.engine.fen,
            "resulting_fen": board.fen(),
            "side_to_move": "white" if board.turn else "black",
            "moves_requested": len(moves),
            "moves_applied": applied,
            "steps": steps,
            "ascii": str(board),
            "is_check": board.is_check(),
            "is_checkmate": board.is_checkmate(),
            "legal_moves_count": board.legal_moves.count(),
        }
        if error is not None:
            payload["error"] = error.to_dict()
            payload["message"] = (
                f"Step {applied + 1} failed: {error.message}"
                f" {error.hint}".strip()
                + " Moves must alternate between white and black starting from the "
                "side to move of the given FEN."
            )
        else:
            payload["message"] = (
                f"All {applied} move(s) are legal. Nothing was committed to the real "
                "game; call make_move with the first move to play it."
            )
        return _json(payload)

    def _tool_make_move(self, args: Dict[str, Any]) -> str:
        move = args.get("move")
        if not isinstance(move, str) or not move.strip():
            raise MoveError(
                CATEGORY_INVALID_INPUT,
                "Invalid input: 'move' must be a single SAN string, e.g. \"Nf3\".",
            )
        san = move.strip()
        legal = self.engine.legal_moves_san()
        if san not in legal:
            raise MoveError(
                CATEGORY_ILLEGAL_MOVE,
                f"Illegal move: '{san}' is not one of the {len(legal)} legal moves "
                "in the current position.",
                detail={"legal_moves_sample": legal[:32]},
            )

        record = self.engine.apply_san(san)
        self.committed_move = record.san
        self.committed_uci = record.uci
        self.terminal = True

        board = self.engine.board
        result = evaluate_position(board, self.draw)
        return _json(
            {
                "ok": True,
                "tool": "make_move",
                "committed": True,
                "move": record.san,
                "uci": record.uci,
                "played_by": record.side,
                "fen": self.engine.fen,
                "side_to_move": self.engine.side_to_move,
                "check_status": self.engine.check_status(),
                "is_check": board.is_check(),
                "is_checkmate": board.is_checkmate(),
                "legal_moves_count": board.legal_moves.count(),
                "game_over": result is not None,
                "result": result.to_dict() if result else None,
                "message": f"Played {record.san}. Your turn is over.",
            }
        )

    def _tool_resign(self, args: Dict[str, Any]) -> str:
        reason = args.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise MoveError(
                CATEGORY_INVALID_INPUT,
                "Invalid input: 'reason' must be a string when provided.",
            )
        self.resigned = True
        self.resign_reason = (reason or "").strip() or None
        self.terminal = True
        return _json(
            {
                "ok": True,
                "tool": "resign",
                "committed": True,
                "resigned_by": self.side,
                "reason": self.resign_reason,
                "message": f"{self.side.capitalize()} resigned. The game is over.",
            }
        )

    # ----------------------------------------------------------------- helpers
    def _parse_arguments(self, raw_arguments: str) -> Dict[str, Any]:
        if raw_arguments is None or str(raw_arguments).strip() in ("", "{}"):
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise MoveError(
                CATEGORY_INVALID_INPUT,
                f"Invalid input: arguments are not valid JSON ({exc.msg}).",
            ) from exc
        if not isinstance(parsed, dict):
            raise MoveError(
                CATEGORY_INVALID_INPUT,
                "Invalid input: tool arguments must be a JSON object.",
            )
        return parsed

    def _error(self, name: str, err: MoveError) -> str:
        LOGGER.info("Tool %s failed (%s): %s", name, err.category, err.message)
        return _json(
            {
                "ok": False,
                "tool": name,
                "error": err.to_dict(),
                "message": f"{err.message} {err.hint}".strip(),
            }
        )
