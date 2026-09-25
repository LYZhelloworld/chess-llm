# Tool contract

Every tool takes JSON arguments and returns a JSON string. Definitions live in
`src/tools/schema.py` (OpenAI function-calling format), execution lives in
`src/tools/executor.py`.

## Error model

A failed call never raises into the agent loop. It returns:

```json
{
  "ok": false,
  "tool": "make_move",
  "error": {
    "category": "illegal_move",
    "message": "Illegal move: 'Nf5' is not one of the 20 legal moves in the current position.",
    "hint": "Call get_legal_moves and pick one of the returned moves."
  },
  "message": "Illegal move: 'Nf5' ... Call get_legal_moves and pick one of the returned moves."
}
```

| Category | Meaning |
| --- | --- |
| `invalid_input` | The argument is not valid JSON, has the wrong type, or the SAN/FEN string cannot be parsed |
| `illegal_move` | Well-formed notation, but the move is not legal in this position |
| `illegal_state` | The action is not allowed now (for example moving after the turn is already committed) |
| `engine_error` | python-chess rejected the request in an unexpected way |
| `unknown_tool` | The model invented a tool name |

## get_board

Arguments: none.

```json
{
  "ok": true,
  "tool": "get_board",
  "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "side_to_move": "white",
  "your_side": "white",
  "check_status": "no check",
  "castling_rights": "KQkq",
  "en_passant": "-",
  "halfmove_clock": 0,
  "fullmove_number": 1,
  "last_move": null,
  "legal_moves_count": 20,
  "game_over": false,
  "result": null
}
```

`check_status` is one of `no check`, `white is in check`, `black is in check`.

## get_board_ascii

Arguments: none. Returns `fen`, `side_to_move` and `ascii`:

```
r n b q k b n r
p p p p p p p p
. . . . . . . .
. . . . . . . .
. . . . . . . .
. . . . . . . .
P P P P P P P P
R N B Q K B N R
```

## get_legal_moves

Arguments: none. Returns SAN strings only:

```json
{"ok": true, "tool": "get_legal_moves", "fen": "...", "side_to_move": "white",
 "count": 20, "moves": ["a3", "b3", "...", "Nf3"], "note": "make_move only accepts one of these exact SAN strings."}
```

## try_make_move

```json
{"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
 "moves": ["e4", "e5", "Nf3"]}
```

- `fen` is optional; the current board is used when it is omitted.
- `moves` needs at least one SAN move and they must alternate sides: if the
  first move belongs to white, the second must belong to black, and so on.
  python-chess enforces this, and the violation is reported with the offending
  step index.
- Nothing is committed; the live board is untouched.

```json
{
  "ok": true,
  "tool": "try_make_move",
  "simulated": true,
  "committed": false,
  "start_fen": "... w KQkq - 0 1",
  "resulting_fen": "... b KQkq - 1 2",
  "side_to_move": "black",
  "moves_requested": 3,
  "moves_applied": 3,
  "steps": [{"index": 0, "san": "e4", "uci": "e2e4", "side": "white", "ok": true}],
  "ascii": "...",
  "is_check": false,
  "is_checkmate": false,
  "legal_moves_count": 29,
  "message": "All 3 move(s) are legal. Nothing was committed to the real game; call make_move with the first move to play it."
}
```

On failure `ok` is `false`, `steps` shows how far the line got, and `message`
explains the alternation rule before it mentions the engine error.

## make_move

```json
{"move": "Nf3"}
```

- Accepts exactly one move, and only a string from `get_legal_moves`.
- On success the move is played on the real board and the turn ends.

```json
{
  "ok": true,
  "tool": "make_move",
  "committed": true,
  "move": "Nf3",
  "uci": "g1f3",
  "played_by": "white",
  "fen": "...",
  "side_to_move": "black",
  "check_status": "no check",
  "is_check": false,
  "is_checkmate": false,
  "legal_moves_count": 20,
  "game_over": false,
  "result": null,
  "message": "Played Nf3. Your turn is over."
}
```

If a single assistant message contains several `make_move` calls, the first one
wins and the rest are answered with `illegal_state` so the API stays happy.

## resign

```json
{"reason": "down a queen with no compensation"}
```

```json
{"ok": true, "tool": "resign", "committed": true, "resigned_by": "black",
 "reason": "down a queen with no compensation",
 "message": "Black resigned. The game is over."}
```
