# chess-llm design

## 1. What this project is

A chess battleground where one or both sides can be an LLM. The LLM never edits
the board directly: it can only call JSON tools (`get_legal_moves`,
`make_move`, ...), so every move it plays is validated by `python-chess`.

Supported matchups:

| White | Black | How it runs |
| --- | --- | --- |
| human | llm | you click in the browser, the model answers |
| llm | llm | the server drives both sides automatically |
| human | human | useful for testing the board without spending tokens |

All sides are chosen from the web UI when you start a new game (or load an old
one). There is no command-line flag for the matchup - the server just starts and
shows an empty board until you pick one.

## 2. Layered architecture

```
UI / front end            src/ui/server.py (FastAPI) + src/ui/static/index.html
        |  REST + SSE
Game service / state machine   src/game/controller.py, src/game/state.py
        |
Rule engine                src/engine/board.py, src/engine/rules.py (python-chess)
        |
LLM agent layer            src/agent/loop.py, src/agent/llm_client.py
        |
Tool calling               src/tools/executor.py, src/tools/schema.py
        |
Persistence                src/game/storage.py (games/, logs/)
```

Dependencies only ever point downwards: the agent layer never imports FastAPI,
the UI never touches python-chess directly, and nothing imports the prompts
module except the agent loop.

### Module map

| Path | Responsibility |
| --- | --- |
| `src/main.py` | CLI: builds or loads a game, starts the web UI or the headless runner |
| `src/config.py` | `config.yaml` + env + CLI overrides, dataclass config objects |
| `src/prompts.py` | **every** prompt string in the project |
| `src/engine/board.py` | `ChessEngine`: FEN, ASCII, legal moves, SAN/UCI parsing, simulation |
| `src/engine/rules.py` | terminal detection (mate, stalemate, draws) and `GameResult` |
| `src/engine/errors.py` | the error taxonomy returned to the LLM |
| `src/tools/schema.py` | OpenAI function-calling tool definitions |
| `src/tools/executor.py` | runs one tool call, always answers with JSON |
| `src/agent/llm_client.py` | OpenAI-compatible client, retries, `AssistantMessage` |
| `src/agent/loop.py` | the turn loop, chat memory and the sliding context window |
| `src/game/state.py` | serialisable `GameState` / `PlayerSpec` |
| `src/game/controller.py` | whose turn it is, post-move bookkeeping, saving |
| `src/game/storage.py` | PGN + JSON writers, log setup, game loading |
| `src/ui/server.py` | REST endpoints and the SSE event stream |
| `src/ui/static/index.html` | single page board UI (no build step) |

## 3. Flow of one move

1. The UI (or the headless runner) asks the controller whose turn it is.
2. **Human**: `POST /api/move` with a SAN or UCI string -> `submit_human_move`.
   **LLM**: `run_llm_turn()` starts the agent loop (see `docs/LLM-LOOP.md`).
3. The move is committed: appended to `moves_san` / `moves_uci`, logged, and
   pushed to the SSE stream as a `move` event.
4. `evaluate_position()` checks checkmate, stalemate, insufficient material,
   repetition, the no-progress rule and the move limit.
5. If the game continues, the PGN and JSON are rewritten; if it ended, the
   result is stored, the files are written and a `game_over` event is emitted.
6. If the next side is an LLM, the server schedules the next turn.

## 4. Decisions worth knowing

- **OpenAI-compatible only.** One code path (`chat.completions` + `tools`)
  covers OpenAI, DeepSeek, Moonshot, vLLM, Ollama and anything else that speaks
  that protocol; switching providers is a `config.yaml` profile.
- **Table talk is injected as context.** Anything the model writes as normal
  text is shown in the web UI, stored in the game JSON / PGN comments, *and*
  forwarded to the opponent as `Opponent's last spoken remark` at the start of
  the opponent's next turn (see `docs/LLM-LOOP.md`). This lets agents respond to
  trash talk, at the cost of exposing them to prompt injection from the other side.
- **Chain of thought is prompt-level.** The system prompt asks the model to
  reason privately and keep public text short; no vendor-specific
  `reasoning_effort` parameter is sent, so the same code works everywhere.
- **`try_make_move` is a sandbox.** It works on a copy of the board (or on a
  caller-supplied FEN) and never mutates the live game.
- **`make_move` is strict.** Only strings returned by `get_legal_moves` are
  accepted, one per call, and the call ends the turn.
- **Failure is never fatal.** A tool error is classified and handed back to the
  model; a backend failure falls back to a random legal move (seeded, so runs
  are reproducible) instead of killing the game.
- **The HTTP API is local-only, with no opt-out.** The launcher refuses to bind
  anything but a loopback address (`src/main.py`, `bind_refusal`), so the port is
  never reachable from the network at all - binding `0.0.0.0` and rejecting
  requests afterwards still leaves the port visible to scanners. A second
  `local_only` middleware checks the `Host` header and answers `403` otherwise,
  which is what stops a page in the browser from reaching the API through DNS
  rebinding; the peer address cannot distinguish those requests, so it is not
  what gets checked. There is no authentication and every LLM turn spends the
  operator's credits.
- **Resigning over the API is a human action.** `POST /api/resign` only accepts
  a human seat (an API caller must not be able to make the model resign), and in
  a hot-seat game only the side to move; the controller enforces this, not just
  the browser. A model resigns itself through its own `resign` tool, which ends
  its turn and is applied by the controller as part of that turn.

## 5. Web UI

- `GET /` - single page app.
- `GET /api/state` - full snapshot: FEN, ASCII, legal moves, move list, both
  chat histories, the merged `conversation` shown in the UI, result.
- `POST /api/move` - `{ "move": "Nf3" }` (SAN or UCI).
- `POST /api/resign` - `{ "side": "white", "reason": "..." }` (reason is
  optional). Rejected with `400` unless the seat is human and - in a hot-seat
  game - is the side to move.
- `POST /api/new` - start a game, choosing human/llm per side.
- `POST /api/load` - `{ "game_id": "game_20260925_195646" }`.
- `GET /api/games` - saved game ids.
- `GET /api/profiles` - every LLM profile from `config.yaml` and whether it is
  configured (has an API key), used to populate the "New game" / "Load" dropdowns.
- `GET /api/events` - server-sent events (`turn_start`, `tool_call`,
  `tool_result`, `llm_message`, `llm_delta`, `move`, `game_over`, ...).
  Streaming events (`llm_delta`) update the UI in place; other events trigger a
  state refresh.

A non-loopback `--host` is refused at startup, and there is no flag to override
that. The `local_only` middleware (`src/ui/server.py`) additionally rejects
requests whose `Host` header does not name this machine, so an app assembled with
`create_app` directly is covered too.

The board is drawn from white's side. When the only human seat is black it is
flipped, so the side the operator actually plays is at the bottom; a hot-seat
game keeps white at the bottom because both players share the screen.

LLM turns run in a worker thread (`asyncio.to_thread`) so the event loop keeps
serving requests, and a lock guarantees only one turn runs at a time. Because a
turn can outlive the game it belongs to, each controller emits through a guard
that drops its events once a new game has replaced it.

## 6. Configuration

`config.yaml` holds LLM profiles (base URL, `api_key`, `api_key_env` fallback,
model, temperature, timeouts, retries, the `reasoning` mode and the `stream`
mode), draw thresholds, loop limits, storage directories and the `server` access
policy. The repo ships `config.example.yaml` only; copy it to `config.yaml` to
start. `config.yaml` is git-ignored because it contains API keys.

The sections are validated on load: a malformed one (for example
`llm_profiles: "openai"` instead of a mapping) raises a `ConfigError` naming the
offending key, rather than failing later with an `AttributeError`.

The server starts **without** `config.yaml`: you can launch it and play
human vs human straight away. A profile is only *required* when you start a game
with an LLM side - you pick it from the web UI's "New game" panel, and the
profile must have an API key set. Starting an LLM game while the chosen profile
has no key (or does not exist) is rejected with a clear message instead of
crashing.

Precedence for the key: `api_key` in the profile → the `api_key_env` variable.
`OPENAI_BASE_URL` / `OPENAI_MODEL` environment variables override the *default*
profile. There are no command-line flags for keys, models, or sides - those are
all chosen in the browser.

## 6.1 Game modes

The four matchups are the same as above, but the sides are selected in the UI
rather than on the command line. The web UI's "New game" panel lets you set
white and black independently to `human` or `llm:<profile>` (where `<profile>`
is a name from `config.yaml`). "Load" replays a saved game and, optionally,
overrides either seat with a different `human` / `llm:<profile>` choice; if you
leave a seat as "(keep stored)" but its saved profile is no longer available,
the load is rejected and you must specify who plays that side.

`human-vs-human` is hot-seat: the seat that may act is simply the side to move
(the UI asks the controller, which rejects a move submitted for the wrong side).

## 7. Related documents

- `docs/TOOLS.md` - the exact JSON contract of every tool.
- `docs/LLM-LOOP.md` - the agent loop, nudges, context window, fallback.
- `docs/PERSISTENCE.md` - PGN, JSON and log formats.
