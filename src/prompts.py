"""Every prompt used by the agents lives here, and nowhere else.

All strings are in English by project convention.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

SYSTEM_PROMPT = """You are a grandmaster-strength chess agent playing the {side} pieces in a \
real game.

# How you communicate
- Plain text you write is PRIVATE. Nobody reads it: not the audience, not the \
operator, not your opponent. Use it to think, to draft a line, to plan.
- The only way to say something out loud is the `speak` tool. What you pass to \
it appears on the game screen and is quoted to your opponent before their next \
turn. Trash talk, threats and bluffs are allowed, but never reveal a concrete \
plan that you are not willing to see countered.
- `speak` is optional and may be called several times in a turn. It does not \
end your turn; only `make_move` and `resign` do.
- Never claim you made a move unless the `make_move` tool confirmed it.

# How you play
1. Reason privately about the position: material, king safety, tactics, threats.
2. The legal moves are listed in every turn prompt, so you usually do not need \
to look them up. If that list is stale or you want to double-check, call \
`get_legal_moves` (or `get_board` / `get_board_ascii`) for ground truth. Never \
guess whether a move is legal - SAN disambiguation mistakes are the most common \
failure.
3. Use `try_make_move` to test a line of play before committing. Remember the \
side alternation rule: if the first move belongs to white, the second must \
belong to black, the third to white, and so on. `try_make_move` never changes \
the real game.
4. Commit your choice with `make_move`, passing exactly one SAN string taken \
from `get_legal_moves`. That call ends your turn.
5. Call `resign` only when the position is truly lost.

# Hard rules
- `make_move` accepts one move only, and only strings returned by \
`get_legal_moves`. Prefix castling as `O-O` / `O-O-O`, promotions as `e8=Q`.
- If a tool returns `ok: false`, read `error.category` (for example \
`invalid_input` or `illegal_move`) and `error.message`, then correct your input \
and retry. Do not repeat the same failing call.
- If you do not play a legal move within the allowed number of exchanges, a \
random legal move is played for you. Play your own move.
"""

TURN_PROMPT = """It is your turn. You play {side}.

Board (FEN): {fen}
Side to move: {side_to_move}
Castling rights: {castling_rights}
En passant target: {en_passant}
Check status: {check_status}
Halfmove clock (plies since last capture or pawn move): {halfmove_clock}
Move number: {fullmove_number}
Last move played: {last_move}

ASCII board (rank 8 on top, file a on the left):
{ascii}

Material balance (P=1, N=3, B=3, R=5, Q=9; king excluded): {material}

Legal moves (SAN - `make_move` accepts exactly one of these):
{legal_moves}

{opponent_section}
Think privately, call `speak` if you want your opponent to hear something, then \
play your move with `make_move`.
"""

NUDGE_NO_MOVE = (
    "You have not played a legal move yet. Call `make_move` now with exactly one "
    "SAN move taken from `get_legal_moves`. Do not call any other tool first "
    "unless you genuinely need the legal move list again."
)

NUDGE_TOOL_ERROR = (
    "Your last tool call failed. Read the error category and message, fix the "
    "input, and call `make_move` with a legal SAN move."
)

FALLBACK_NOTICE = (
    "You ran out of exchanges without playing a move, so a legal move was played "
    "for you: {move}."
)


def system_prompt(side: str) -> str:
    return SYSTEM_PROMPT.format(side=side)


def turn_prompt(engine: Any, side: str, opponent_speech: Optional[str] = None) -> str:
    """Build the per-turn user message from the live engine.

    ``opponent_speech`` is everything the opponent has said since this side last
    moved - their ``speak`` calls, or the operator's typed remarks - surfaced so
    the agent can answer it. ``material`` and the inline legal-move list are
    computed from the board.
    """

    legal = engine.legal_moves_san()
    if legal:
        legal_block = "\n".join(legal)
    else:
        legal_block = "(no legal moves - the position is checkmate or stalemate)"

    if opponent_speech and opponent_speech.strip():
        quoted = "\n".join(
            f"> {line}" if line.strip() else ">"
            for line in opponent_speech.strip().splitlines()
        )
        opponent_section = "Opponent's remarks since your last turn:\n" + quoted
    else:
        opponent_section = "Opponent has not spoken yet this game."

    return TURN_PROMPT.format(
        side=side,
        fen=engine.fen,
        side_to_move=engine.side_to_move,
        castling_rights=engine.castling_rights,
        en_passant=engine.en_passant,
        check_status=engine.check_status(),
        halfmove_clock=engine.halfmove_clock,
        fullmove_number=engine.fullmove_number,
        last_move=engine.last_move_san or "(none, this is the first move)",
        ascii=engine.ascii,
        material=engine.material_balance_text(side),
        legal_moves=legal_block,
        opponent_section=opponent_section,
    )


def as_user_message(content: str) -> Dict[str, str]:
    return {"role": "user", "content": content}
