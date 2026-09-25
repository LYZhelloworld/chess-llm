"""Tool definitions in the OpenAI function-calling schema.

All tools take JSON arguments and return JSON payloads, so a single definition
set works for every OpenAI-compatible backend.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

TOOL_NAMES = (
    "get_board",
    "get_board_ascii",
    "get_legal_moves",
    "try_make_move",
    "make_move",
    "resign",
)

_EMPTY: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_board",
            "description": (
                "Return the current board state in FEN format, together with "
                "castling rights, en passant target, check status and counters."
            ),
            "parameters": _EMPTY,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_board_ascii",
            "description": (
                "Return the current board state as an ASCII picture. Rank 8 is on "
                "the first line, file a is on the left. Use it to double check a "
                "position you are unsure about."
            ),
            "parameters": _EMPTY,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_legal_moves",
            "description": (
                "Return every legal move in the current position, in SAN format "
                "only. make_move accepts nothing else."
            ),
            "parameters": _EMPTY,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "try_make_move",
            "description": (
                "Dry-run a sequence of one or more SAN moves and see the resulting "
                "FEN. Moves must alternate sides: if the first move belongs to "
                "white, the second must belong to black, the third to white, and so "
                "on. Nothing is committed to the real game; use make_move to play "
                "for real."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fen": {
                        "type": "string",
                        "description": (
                            "Optional starting FEN. Defaults to the current board."
                        ),
                    },
                    "moves": {
                        "type": "array",
                        "description": "SAN moves to apply, at least one, alternating sides.",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                },
                "required": ["moves"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_move",
            "description": (
                "Play exactly one move in the real game. The move must be one of "
                "the SAN strings returned by get_legal_moves. This ends your turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "move": {
                        "type": "string",
                        "description": "One legal move in SAN format, e.g. Nf3, exd5, O-O, e8=Q.",
                    }
                },
                "required": ["move"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resign",
            "description": (
                "Give up the game when your position is hopeless. Only use it after "
                "careful thought; the loss is recorded permanently."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short explanation shown to the audience.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
]


def get_tool_definitions(names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if not names:
        return list(TOOL_DEFINITIONS)
    wanted = set(names)
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in wanted]
