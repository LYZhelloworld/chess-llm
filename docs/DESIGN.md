# chess-llm design

## 1. What this project is

A chess battleground where one or both sides can be an LLM. The LLM never edits
the board directly: it can only call JSON tools (`get_legal_moves`,
`make_move`, ...), so every move it plays is validated by `python-chess`.

Supported matchups:

| White | Black | How it runs |
| --- | --- | --- |
| human | llm | default: you click in the browser, the model answers |
| llm | llm | the server drives both sides automatically |
| human | human | useful for testing the board without spending tokens |
| llm | llm (headless) | `--headless`, no browser at all |

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
- **Table talk is display-only.** Anything the model writes as normal text is
  shown in the web UI and stored in the game JSON / PGN comments, but it is
  *not* injected into the opponent's context. This keeps the opponent immune to
  prompt injection and keeps contexts small. Change `docs/LLM-LOOP.md` if you
  want to reverse this.
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

## 5. Web UI

- `GET /` - single page app.
- `GET /api/state` - full snapshot: FEN, ASCII, legal moves, move list, both
  chat histories, the merged `conversation` shown in the UI, result.
- `POST /api/move` - `{ "move": "Nf3" }` (SAN or UCI).
- `POST /api/resign` - `{ "side": "white" }`.
- `POST /api/new` - start a game, choosing human/llm per side.
- `POST /api/load` - `{ "game_id": "game_20260925_195646" }`.
- `GET /api/games` - saved game ids.
- `GET /api/events` - server-sent events (`turn_start`, `tool_call`,
  `tool_result`, `llm_message`, `move`, `game_over`, ...). The UI refetches the
  state whenever an event arrives.

LLM turns run in a worker thread (`asyncio.to_thread`) so the event loop keeps
serving requests, and a lock guarantees only one turn runs at a time.

## 6. Configuration

`config.yaml` holds LLM profiles (base URL, `api_key`, `api_key_env` fallback,
model, temperature, timeouts, retries and the `reasoning` mode), draw
thresholds, loop limits and storage directories. It is **required**: the repo
ships `config.example.yaml` only, and
`src/config.py` raises `ConfigError` (the CLI prints the copy instructions and
exits with code 2) when `config.yaml` is missing. `config.yaml` is git-ignored
because it contains API keys.

Precedence for the key: `api_key` in the profile → the `api_key_env` variable →
`--api-key`. `OPENAI_BASE_URL` / `OPENAI_MODEL` override the default profile,
and CLI flags override everything. See `README.md` for the flag list.

The file is only *required* when at least one side is an LLM: `src.main` loads
the config with `required=False`, then errors out after the `GameState` is
built if `config.yaml` is missing and a side is an LLM. A `human-vs-human`
game therefore starts with no config at all (a notice is printed), which keeps
the board usable before any endpoint exists.

## 6.1 Game modes

`--mode` is a shortcut over `--white` / `--black` (which still win when given):

| Mode | White | Black | Needs config |
| --- | --- | --- | --- |
| `human-vs-llm` (default) | human | LLM | yes |
| `llm-vs-human` | LLM | human | yes |
| `llm-vs-llm` | LLM | LLM | yes |
| `human-vs-human` | human | human | no |

`human-vs-human` is hot-seat: the seat that may act is simply the side to move
(the UI asks the controller, which rejects a move submitted for the wrong
side). `--headless` still requires both sides to be LLMs.

## 7. Related documents

- `docs/TOOLS.md` - the exact JSON contract of every tool.
- `docs/LLM-LOOP.md` - the agent loop, nudges, context window, fallback.
- `docs/PERSISTENCE.md` - PGN, JSON and log formats.
