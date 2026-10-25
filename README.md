# chess-llm

English | [简体中文](README_zh.md)

Play chess against an LLM, or watch two LLMs play each other. The model never
writes to the board directly: it can only call JSON tools
(`get_board`, `get_board_ascii`, `get_legal_moves`, `make_move`, `resign`, ...), and every move is validated by
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
Tools                     get_board / get_board_ascii / get_legal_moves / try_make_move / make_move / resign
```

## Install

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml                # then put your API key in it
```

Requires Python 3.10+. The server starts even without `config.yaml` — you can
play human vs human straight away. A `config.yaml` is only needed once you start
a game with an LLM side; it is git-ignored because it holds your API keys.

## Run

```bash
python -m src.main
# opens http://127.0.0.1:8000
```

Then, in the browser:

- **New game** — pick who plays white and black. Each side is either `human` or
  an LLM profile from `config.yaml`; an LLM side needs a profile that has an API
  key configured.
- **Load saved game** — pick a previous game; optionally override either seat
  (for example to swap in a different profile, or to take over a seat yourself).
- Click pieces (or type SAN) to move when it is a human's turn, and watch both
  agents' merged conversation stream in live. The chain of thought for each turn is
  shown in a collapsible block (expanded while the model is still thinking, collapsed
  once its reply starts; past turns start collapsed).

`human-vs-human` is hot-seat: both players share the board, and whoever is to
move may play; the resign button acts on the side to move.

## Command line

The command line only carries what the server needs. The matchup itself is
chosen in the browser.

| Flag | Meaning |
| --- | --- |
| `--config` | path to `config.yaml` (copied from `config.example.yaml`) |
| `--host` / `--port` | web server binding (default 127.0.0.1:8000) |
| `--log-level` | DEBUG / INFO / WARNING / ERROR |

## Configuration

The model is configured in `config.yaml`, with environment variables as an
optional override for the default profile.

### 1. `config.yaml` (recommended)

Create it once by copying the template:

```bash
cp config.example.yaml config.yaml
```

```yaml
llm_profiles:
  default:   {base_url: "https://api.openai.com/v1",  api_key: "sk-...", model: gpt-4o-mini}
  deepseek:  {base_url: "https://api.deepseek.com/v1", api_key: "sk-...", model: deepseek-chat}
  local:     {base_url: "http://127.0.0.1:11434/v1",   api_key: "sk-no-key", model: qwen2.5:14b, temperature: 0.6, reasoning: auto}

draw:    {repetition_count: 3, no_progress_moves: 50, max_game_moves: 200}
loop:    {max_tool_turns: 20, context_turns: 20}
storage: {games_dir: games, logs_dir: logs, log_level: INFO}
server:  {allow_remote: false}
```

`api_key` holds the key itself and takes priority. If you prefer to keep secrets
out of the file, leave `api_key: ""` and set `api_key_env: OPENAI_API_KEY` —
that is only the *name* of an environment variable to read.

`reasoning` controls the chain of thought a reasoning model returns
(`reasoning_content`, `reasoning`, ...). It is always stored in the game JSON
and shown in the UI inside a collapsible block; the mode says whether it is also sent back
with the history: `auto` (default) tries it and stops if the backend refuses,
`on` always sends it, `off` never sends it.

When you start a game in the UI, choose a profile per side from the dropdowns —
there is no command-line equivalent anymore.

`server.allow_remote` (default `false`) decides whether the HTTP API accepts
requests from other machines. The API has no authentication and every LLM turn
spends your credits, so the default is to listen on `127.0.0.1` only: passing a
non-loopback `--host` while the flag is `false` is refused at startup instead of
binding the port and answering `403`. Set it to `true` if you really want the
network to reach it. Opening it up is your responsibility.

### 2. Environment variables (optional)

Used only by profiles whose `api_key` is empty. `OPENAI_BASE_URL` and
`OPENAI_MODEL` additionally override the default profile's endpoint and model:

```bash
export OPENAI_API_KEY=sk-...     # read by the "default" profile via api_key_env
export OPENAI_BASE_URL=...       # optional override of base_url
export OPENAI_MODEL=gpt-4o       # optional override of model
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
