# AGENTS.md

Working conventions for this repository. Design details are **not** repeated
here - read the docs instead.

## Read first

- [`docs/README.md`](docs/README.md) - English documentation index
- [`docs/DESIGN.md`](docs/DESIGN.md) - architecture, module map, key decisions
- [`docs/TOOLS.md`](docs/TOOLS.md) - JSON contract of every LLM tool
- [`docs/LLM-LOOP.md`](docs/LLM-LOOP.md) - agent loop, nudges, context window
- [`docs/PERSISTENCE.md`](docs/PERSISTENCE.md) - PGN / JSON / log formats
- [`README.md`](README.md) - install, run, CLI flags

## Commands

```bash
python -m pytest -q                       # run the test suite
python -m src.main                        # human vs LLM, http://127.0.0.1:8000
python -m src.main --white llm --black llm --headless
python tests/fake_llm_server.py --port 8099   # stub backend for local smoke tests
```

## Rules for agents and humans

1. **English only.** Code, comments, docstrings, logs, docs and prompts are
   English. This is a hard requirement of the project. The only exceptions are
   the Chinese translations `README_zh.md` and `docs-zh/`, which mirror the
   English documents and must be kept in sync when those change.
2. **All prompts live in `src/prompts.py`.** Never inline a prompt string
   anywhere else, not even in tests.
3. **The rule engine is the only source of truth.** Never compute legality,
   check status or terminal results by hand - call `src/engine`.
4. **Tools take JSON and return JSON.** A tool must never raise into the agent
   loop; classify the failure (`invalid_input`, `illegal_move`,
   `illegal_state`, `engine_error`, `unknown_tool`) and return it.
5. **Keep the layers one-directional.** `ui` -> `game` -> `engine`; `agent`
   depends on `tools` and `engine` only. No module imports upwards.
6. **Never lose a game.** Anything that can fail (LLM backend, file write, event
   delivery) must be caught and degraded, and the game must still be saved on
   exit.
7. **Persistence is append-safe.** PGN and JSON are rewritten in full after
   every move; logs are appended. Keep it that way.

## Where to make a change

| Task | File |
| --- | --- |
| New or reworded prompt | `src/prompts.py` |
| New tool | `src/tools/schema.py` + `_tool_*` in `src/tools/executor.py` |
| New draw condition | `src/engine/rules.py` + `DrawConfig` in `src/config.py` |
| Loop behaviour (turns, nudges, fallback) | `src/agent/loop.py` |
| Turn order, post-move bookkeeping | `src/game/controller.py` |
| Storage format | `src/game/storage.py` |
| REST/SSE or the board UI | `src/ui/` |

## Testing

Not mandatory, but new engine, tool and loop behaviour should come with a
`pytest` case under `tests/`. Use a scripted fake client (see
`tests/test_controller.py`) instead of calling a real model. Game records in
`games/` and `logs/` are working artifacts, not fixtures.
