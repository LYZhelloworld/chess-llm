# chess-llm

English | [简体中文](README_zh.md)

Play chess against an LLM, or watch two LLMs play each other. The model never
writes to the board directly: it can only call JSON tools
(`get_legal_moves`, `make_move`, `resign`, ...), and every move is validated by
[python-chess](https://python-chess.readthedocs.io/).

```
UI / front end            FastAPI + single page HTML board
        |
Game service / state machine
        |
Rule engine               python-chess
        |
LLM agent layer           OpenAI-compatible tool calling
        |
Tools                     get_board / get_legal_moves / try_make_move / make_move / resign
```

## Install

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml                # then put your API key in it
```

Requires Python 3.10+. Any game with an LLM side refuses to start until
`config.yaml` exists — it is git-ignored because it holds your API keys.
`--mode human-vs-human` is the exception: it runs with no config file at all,
which is handy while you are still setting up (or downloading) a model.

## Run

```bash
# human (white) vs LLM (black) - open http://127.0.0.1:8000
python -m src.main

# LLM vs LLM in the browser
python -m src.main --mode llm-vs-llm

# LLM vs LLM without a browser (prints each board, then the result)
python -m src.main --mode llm-vs-llm --headless

# human vs human on one screen (hot-seat): needs no model and no config.yaml
python -m src.main --mode human-vs-human

# resume a saved game
python -m src.main --load game_20260925_195646
```

`--mode` is just a shortcut for the two sides; `--white` / `--black` still win
if you pass them. In `human-vs-human` both players use the same board: whoever
is to move may play, and the resign button acts on the side to move.

The web UI lets you click pieces (or type SAN), read what both agents said in
one merged stream (prompts, tool calls and tool results are left out; chain of
thought is behind a "show chain of thought" toggle), start a new game, and load
a saved one.

## Command line

| Flag | Meaning |
| --- | --- |
| `--mode` | `human-vs-llm`, `llm-vs-human`, `llm-vs-llm`, `human-vs-human` |
| `--white` / `--black` | `human` or `llm` (default: human / llm; overrides `--mode`) |
| `--white-profile` / `--black-profile` | named profile from `config.yaml` |
| `--model`, `--white-model`, `--black-model` | model override |
| `--base-url` | OpenAI-compatible endpoint (DeepSeek, Moonshot, vLLM, Ollama, ...) |
| `--api-key` | API key override for both LLM sides |
| `--api-key-env` | env var that holds the API key |
| `--config` | path to a different `config.yaml` |
| `--load` | resume `game_yyyyMMdd_HHmmss` |
| `--seed` | seed for the random-fallback move |
| `--headless` | no web UI (both sides must be LLMs) |
| `--host` / `--port` | web server binding (default 127.0.0.1:8000) |
| `--log-level` | DEBUG / INFO / WARNING / ERROR |

## Configuration

There are three ways to configure the model, applied in this order (later wins):
`config.yaml` → environment variables → CLI flags.

### 1. `config.yaml` (recommended)

Create it once by copying the template:

```bash
cp config.example.yaml config.yaml
```

```yaml
default_profile: default          # used when no --*-profile is given

llm_profiles:
  default:   {base_url: "https://api.openai.com/v1",  api_key: "sk-...", model: gpt-4o-mini}
  deepseek:  {base_url: "https://api.deepseek.com/v1", api_key: "sk-...", model: deepseek-chat}
  local:     {base_url: "http://127.0.0.1:11434/v1",   api_key: "sk-no-key", model: qwen2.5:14b, temperature: 0.6, reasoning: auto}

draw:    {repetition_count: 3, no_progress_moves: 50, max_game_moves: 200}
loop:    {max_tool_turns: 20, context_turns: 20}
storage: {games_dir: games, logs_dir: logs, log_level: INFO}
```

`api_key` holds the key itself and takes priority. If you prefer to keep secrets
out of the file, leave `api_key: ""` and set `api_key_env: OPENAI_API_KEY` —
that is only the *name* of an environment variable to read.

`reasoning` controls the chain of thought a reasoning model returns
(`reasoning_content`, `reasoning`, ...). It is always stored in the game JSON
and shown in the UI behind a toggle; the mode says whether it is also sent back
with the history: `auto` (default) tries it and stops if the backend refuses,
`on` always sends it, `off` never sends it.

Use a profile per side:

```bash
python -m src.main --white-profile deepseek --black-profile local
```

### 2. Environment variables (optional)

Used only by profiles whose `api_key` is empty. `OPENAI_BASE_URL` and
`OPENAI_MODEL` additionally override the default profile's endpoint and model:

```bash
export OPENAI_API_KEY=sk-...     # read by the "default" profile via api_key_env
export OPENAI_BASE_URL=...       # optional override of base_url
export OPENAI_MODEL=gpt-4o       # optional override of model
```

### 3. CLI flags (quickest for one-off experiments)

```bash
python -m src.main --model gpt-4o                       # both sides
python -m src.main --api-key sk-...                     # key for both sides
python -m src.main --base-url http://127.0.0.1:11434/v1 --model qwen2.5:14b --api-key sk-no-key
```

Any OpenAI-compatible endpoint works (OpenAI, DeepSeek, Moonshot, vLLM, Ollama,
LM Studio, ...) because only `chat.completions` + function calling is used.

Draws are declared on threefold repetition, 50 full moves without a capture or
pawn move, insufficient material, stalemate, and a hard cap of 200 full moves.

## How an agent turn works

1. The agent receives its side, FEN, castling rights, check status, counters
   and an ASCII board.
2. It may talk (public, shown in the UI) and call tools. Its private
   chain-of-thought is never shown.
3. If it has not played a legal move yet, it is nudged to use `make_move`.
4. After 20 exchanges without a move, a random legal move is played for it.

Details: [`docs/LLM-LOOP.md`](docs/LLM-LOOP.md).

## Files produced

```
games/game_yyyyMMdd_HHmmss.pgn    rewritten after every move, with agent remarks as comments
games/game_yyyyMMdd_HHmmss.json   mode, moves, result, and both full chat histories
logs/log_yyyyMMdd_HHmmss.log      every line carries a timestamp prefix
```

## Tests

```bash
python -m pytest -q
```

## Documentation

- [`docs/`](docs/README.md) - English documentation index
- [`docs/DESIGN.md`](docs/DESIGN.md) - architecture, module map, decisions
- [`docs/TOOLS.md`](docs/TOOLS.md) - JSON contract of every tool
- [`docs/LLM-LOOP.md`](docs/LLM-LOOP.md) - agent loop, nudges, context window
- [`docs/PERSISTENCE.md`](docs/PERSISTENCE.md) - PGN / JSON / log formats
- [`AGENTS.md`](AGENTS.md) - conventions for working on this repo
