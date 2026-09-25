"""Error taxonomy shared by the rule engine and the tool layer.

Every failure surfaced to an LLM carries a stable ``category`` so the model can
tell "my input was malformed" apart from "that move breaks the rules".
"""

from __future__ import annotations

from typing import Any, Dict, Optional

CATEGORY_INVALID_INPUT = "invalid_input"
CATEGORY_ILLEGAL_MOVE = "illegal_move"
CATEGORY_ILLEGAL_STATE = "illegal_state"
CATEGORY_ENGINE_ERROR = "engine_error"
CATEGORY_UNKNOWN_TOOL = "unknown_tool"

HINTS: Dict[str, str] = {
    CATEGORY_INVALID_INPUT: "Fix the argument format and retry. Standard Algebraic "
    "Notation looks like e4, Nf3, O-O, exd5, Qxd8+, e8=Q.",
    CATEGORY_ILLEGAL_MOVE: "Call get_legal_moves and pick one of the returned moves.",
    CATEGORY_ILLEGAL_STATE: "This action is not allowed in the current turn state.",
    CATEGORY_ENGINE_ERROR: "The rule engine rejected the request; retry with a simpler input.",
    CATEGORY_UNKNOWN_TOOL: "Use one of the documented tools.",
}


class MoveError(Exception):
    """Structured, LLM friendly error raised by the rule engine."""

    def __init__(
        self,
        category: str,
        message: str,
        hint: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.hint = hint or HINTS.get(category, "")
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "category": self.category,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.detail:
            payload["detail"] = self.detail
        return payload


def classify_exception(exc: BaseException, context: Optional[Dict[str, Any]] = None) -> MoveError:
    """Translate any exception coming out of python-chess into a MoveError."""

    message = str(exc) or exc.__class__.__name__
    module = type(exc).__module__ or ""
    name = type(exc).__name__

    if isinstance(exc, (TypeError, AttributeError)):
        return MoveError(CATEGORY_INVALID_INPUT, f"Invalid input: {message}", detail=context)
    if name == "AmbiguousMoveError" or module.startswith("chess"):
        if name in {"AmbiguousMoveError", "InvalidMoveError"}:
            return MoveError(
                CATEGORY_INVALID_INPUT,
                f"Could not parse the move notation: {message}",
                detail=context,
            )
        if name == "IllegalMoveError":
            return MoveError(
                CATEGORY_ILLEGAL_MOVE,
                f"Illegal move: {message}",
                detail=context,
            )
        if isinstance(exc, ValueError):
            return MoveError(
                CATEGORY_INVALID_INPUT, f"Invalid input: {message}", detail=context
            )
    if isinstance(exc, ValueError):
        return MoveError(CATEGORY_INVALID_INPUT, f"Invalid input: {message}", detail=context)

    return MoveError(
        CATEGORY_ENGINE_ERROR,
        f"Rule engine error ({name}): {message}",
        detail=context,
    )
