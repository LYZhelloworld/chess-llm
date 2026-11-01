# Persistence: games/ and logs/

Game id = `game_yyyyMMdd_HHmmss`. It is the base name of every artifact of a
game and the id you pick from the web UI's "Load saved game" dropdown.

```
games/game_20260925_195646.pgn     rewritten after every move
games/game_20260925_195646.json    full state + both chat histories
logs/log_20260925_195646.log       append-only, prefixed on every line
```

## PGN (`games/*.pgn`)

Rewritten from scratch after every move, so the file is always a valid,
complete game. Headers: `Event`, `Site`, `Date`, `Round`, `White`, `Black`,
`WhiteKind`, `BlackKind`, `WhiteModel`, `BlackModel`, `Result`, `Termination`,
`PlyCount` (plus `FEN`/`SetUp` when the game did not start from the initial
position). What each side said before its move is attached as a move comment,
derived from the chat history of the side that played it:

```
1. e4 { Let me check the legal moves first. | I will play e4. } 1... e5 2. Nf3 1/2-1/2
```

Only remarks from that side's *own* turn land there. A line the human types
while the opponent is thinking is part of the conversation, but it is not that
opponent's move comment.

## Game JSON (`games/*.json`)

```json
{
  "game_id": "game_20260925_195646",
  "created_at": "2026-09-25T19:56:46",
  "mode": "human-vs-llm",
  "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "white": {"kind": "human", "profile": "", "model": "", "label": ""},
  "black": {"kind": "llm", "profile": "openai", "model": "gpt-4o-mini", "label": ""},
  "moves_san": ["e4", "e5"],
  "moves_uci": ["e2e4", "e7e5"],
  "white_chat_history": [
    {"role": "user", "content": "That is a bold pawn.", "spoken": true, "seq": 1, "ply": 1}
  ],
  "black_chat_history": [
    {"role": "user", "content": "It is your turn. You play black. ..."},
    {"role": "assistant", "content": "", "reasoning": "If I take on e4 ...", "seq": 2, "ply": 2, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "speak", "arguments": "{\"text\": \"Hold my rook.\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "speak", "content": "{\n  \"ok\": true, ... }"}
  ],
  "result": {"winner": null, "reason": "repetition", "detail": "...", "pgn_result": "1/2-1/2"},
  "finished": true
}
```

Rules:

- Chat histories are **complete**, even the parts that are no longer sent to
  the model. `user`, `assistant` and `tool` messages are all stored; the system
  prompt is not.
- `white_chat_history` and `black_chat_history` are both written in every
  matchup - for LLM vs LLM both are populated.
- **A remark is not a field of its own.** A model's remark is the `text`
  argument of a `speak` tool call on an assistant message; a human's remark is a
  `user` message flagged `"spoken": true`. There is no separate talk list, and
  the assistant's `content` is private text that is never displayed.
- An assistant message carries a `reasoning` key when the backend returned a
  chain of thought (`reasoning_content`, `reasoning`, `thinking`, ...). It is
  stored for debugging only.
- Three bookkeeping keys are written next to a remark and stripped again before
  the history goes back to a backend: `seq` puts both sides' remarks on one
  timeline (the two histories are separate lists, so a line typed while the
  opponent was thinking could not be placed otherwise), `ply` is the 1-based ply
  the remark belongs to (that is what the PGN comment uses; `0` means it was
  said after the game ended, so it belongs to no move and never becomes a
  comment), and `spoken` marks a human's `user` message. See `_for_backend()` in
  `src/agent/loop.py`.
- The UI's conversation and the PGN comments are both derived from these two
  histories, so nothing can drift: the same stored remark feeds both.
- `moves_uci` is kept alongside `moves_san` so a move can be replayed even if
  the SAN is ambiguous.
- Games saved before the `speak` tool existed have no remarks at all, and open
  with an empty conversation. Their `content` is not used as a fallback.

## Logs (`logs/*.log`)

The root logger is reconfigured per game by `setup_logging()` in
`src/game/storage.py`. `MultilineFormatter` repeats the timestamp / level /
logger prefix on **every** line of a multi-line record so tracebacks and ASCII
boards stay aligned:

```
2026-09-25 19:56:46 | INFO  | src.game.controller      | Move 1: e4 (e2e4) played by white
2026-09-25 19:56:46 | INFO  | src.tools.executor       | Tool make_move failed (illegal_move): ...
2026-09-25 19:56:46 | ERROR | src.game.controller      | Traceback (most recent call last):
2026-09-25 19:56:46 | ERROR | src.game.controller      |   File "...", line 12, in run
2026-09-25 19:56:46 | ERROR | src.game.controller      | RuntimeError: boom
```

Noisy third-party loggers (`httpx`, `httpcore`, `openai._base_client`) and
uvicorn access lines are filtered out.

## Resuming a game

From the web UI, choose a game in "Load saved game" and click Load (optionally
overriding either seat). The server reads the JSON, rebuilds the board by
replaying the stored moves, restores both chat histories and keeps writing to the
**same** log file (`log_20260925_195646.log`). If the game id does not contain a
parseable timestamp, the current time is used instead. The game is always saved
before the process exits (FastAPI shutdown hook), so Ctrl-C never loses a move.
