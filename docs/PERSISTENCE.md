# Persistence: games/ and logs/

Game id = `game_yyyyMMdd_HHmmss`. It is the base name of every artifact of a
game and the value passed to `--load`.

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
position). Each agent's public remarks for that ply are attached as a move
comment, derived from the chat history of the side that played it, e.g.:

```
1. e4 { Let me check the legal moves first. | I will play e4. } 1... e5 2. Nf3 1/2-1/2
```

## Game JSON (`games/*.json`)

```json
{
  "game_id": "game_20260925_195646",
  "created_at": "2026-09-25T19:56:46",
  "mode": "human-vs-llm",
  "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "white": {"kind": "human", "profile": "default", "model": "", "label": ""},
  "black": {"kind": "llm", "profile": "default", "model": "gpt-4o-mini", "label": ""},
  "moves_san": ["e4", "e5"],
  "moves_uci": ["e2e4", "e7e5"],
  "white_chat_history": [],
  "black_chat_history": [
    {"role": "user", "content": "It is your turn. You play black. ..."},
    {"role": "assistant", "content": "Checking my options.", "reasoning": "If I take on e4 ...", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_legal_moves", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "get_legal_moves", "content": "{\n  \"ok\": true, ... }"}
  ],
  "result": {"winner": null, "reason": "repetition", "detail": "...", "pgn_result": "1/2-1/2"},
  "finished": true,
  "seed": null
}
```

Rules:

- Chat histories are **complete**, even the parts that are no longer sent to
  the model. `user`, `assistant` and `tool` messages are all stored; the system
  prompt is not.
- `white_chat_history` and `black_chat_history` are both written in every
  matchup - for LLM vs LLM both are populated.
- An assistant message carries a `reasoning` key when the backend returned a
  chain of thought (`reasoning_content`, `reasoning`, `thinking`, ...). It is
  stored next to `content` and is the only place remarks live - there is no
  separate talk list.
- PGN move comments and the UI's merged conversation are both derived from
  these histories: a ply maps to the side that played it and to that side's
  n-th turn (a turn starts at every `user` message).
- `moves_uci` is kept alongside `moves_san` so a move can be replayed even if
  the SAN is ambiguous.

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

`python -m src.main --load game_20260925_195646` reads the JSON, rebuilds the
board by replaying the stored moves, restores both chat histories and keeps
writing to the **same** log file (`log_20260925_195646.log`). If the game id
does not contain a parseable timestamp, the current time is used instead. The
game is always saved before the process exits (`atexit` + FastAPI shutdown
hook), so Ctrl-C never loses a move.
