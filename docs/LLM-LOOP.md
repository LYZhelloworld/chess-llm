# The LLM turn loop

Implemented in `src/agent/loop.py` (`run_llm_turn`). One call = one move.

## 1. Prompting

Messages sent to the backend for every exchange:

```
[ system  ] system_prompt(side)                      <- src/prompts.py
[ ...     ] memory.window(loop.context_turns)        <- sliding window of history
```

The system prompt covers: the side being played, that plain text is private while
`speak` is public, the tool workflow, the hard rules (one move per `make_move`,
only SAN from `get_legal_moves`, read `error.category` on failure), and the fact
that a random move is played if the turn budget runs out.

At the start of the turn a `user` message (`turn_prompt`) is appended:

```
It is your turn. You play white.

Board (FEN): rnbqkbnr/...
Side to move: white
Castling rights: KQkq
En passant target: -
Check status: no check
Halfmove clock (plies since last capture or pawn move): 0
Move number: 1
Last move played: (none, this is the first move)

ASCII board (rank 8 on top, file a on the left):
r n b q k b n r
...

Material balance (P=1, N=3, B=3, R=5, Q=9; king excluded): White 39 - Black 39 (material is level); you are level.

Legal moves (SAN - `make_move` accepts exactly one of these):
a3
b3
...
e4
...

Opponent has not spoken yet this game.
```

The legal moves and a per-turn material balance are included inline so the
model rarely needs `get_legal_moves`; that tool (and `get_board` /
`get_board_ascii`) stays available when the list is stale or needs checking.
The opponent's remarks since this side last moved are appended too, so the
agent can respond to table talk.

## 2. The loop

```
for exchange in 1 .. loop.max_tool_turns (default 20):
    response = client.chat(system + window, tools)
    store the assistant message (content + tool_calls)

    if the response has no tool calls:
        append the nudge "You have not played a legal move yet. Call
        make_move now with exactly one SAN move ..."
        continue

    for each tool call:
        result = executor.execute(name, arguments)     # always JSON
        append a role="tool" message with that JSON

    if the executor reached a terminal action (make_move or resign):
        stop and report the outcome
```

Exit conditions:

1. `make_move` succeeded -> the move is committed by the controller.
2. `resign` -> the game ends with a resignation result.
3. The exchange budget is exhausted -> a **random legal move** is played for the
   agent, logged as `[random fallback]`, and a user message telling the model
   what happened is appended so the transcript stays coherent. A turn has to
   change the board: one that ends without a move leaves the same side to move,
   and the driver would start it again immediately.
4. The backend fails after all retries -> same random-move fallback, with the
   error recorded in `TurnOutcome.error`.

## 3. Context window

- The **full** history is kept in `ChatMemory` and persisted to the game JSON
  (`white_chat_history` / `black_chat_history`), including user, assistant and
  tool messages (never the system prompt).
- Only the last `loop.context_turns` (default 20) turns are sent. A *turn* is
  one `user` message plus everything that follows it until the next `user`
  message, which guarantees a `tool_calls` block is never separated from its
  `tool` results. Leading orphan tool messages are dropped as a safety net.

## 4. Table talk

A model's words only reach anyone through the `speak` tool. What it writes as
plain text - including the chain of thought - is private: it is kept in the
transcript for debugging and for the model's own memory, and displayed nowhere.

Each accepted `speak` call:

- is emitted as a `say` event,
- goes into that side's chat history as a normal assistant `tool_calls` entry,
  so nothing extra has to be stored,
- becomes a PGN move comment when the side plays the move it was said before.

Chain of thought is captured from `reasoning_content` (or `reasoning` /
`thinking` / `think`) and stored on the assistant message under `reasoning`. The
profile field `reasoning` decides whether it is sent back with the history:
`auto` (default) sends it and stops if the backend refuses the request, `on`
always sends it, `off` never sends it. In `auto` mode a refused request is
retried once without the chain of thought, and only if that also fails does the
turn fall back to a random move as usual. The reasoning is never displayed.

Speech **is** injected into the opponent's context. `GameController` collects
everything the opponent said since this side last moved - the opponent's `speak`
calls, or the operator's typed remarks - and puts it in the turn prompt as
`Opponent's remarks since your last turn:`. Each remark is delivered once: the
watermark advances when the prompt is built, so a repeated line does not haunt
every later turn. A silent opponent reads as "Opponent has not spoken yet this
game."

A human may type at any time, including while the opponent is thinking. The
remark is stored and displayed immediately; it joins the opponent's prompt at
the start of their **next** turn, because a turn that is already running has
already built its prompt.

## 5. Last words

The turn returns the moment the committing tool runs, so a model that has just
delivered mate - or resigned - would otherwise never get to react to it. When
the position is terminal after that tool, `run_llm_turn` sends **one** extra
request (see `farewell_prompt` in `src/prompts.py`):

- the prompt states the result from that side's point of view, allows a closing
  remark through `speak`, and forbids every other tool ("the board is closed");
- the executor is still in its terminal state, so a `make_move` or `resign` in
  that answer comes back as `illegal_state` rather than changing anything;
- exactly one request is made. There is no retry, no nudge and no loop: the
  result is decided, so anything more would only be latency. A backend failure
  is logged and swallowed, and the move that ended the game is still recorded.

Those remarks carry `ply = 0`, which is the marker for "said after the game":
they are shown in the thread but never become a PGN comment, because they
belong to no move. The same applies to a human's remark typed after the game
ended, which the controller still accepts.

## 6. Events emitted

| Type | Payload |
| --- | --- |
| `user_prompt` | the turn prompt sent to the model |
| `llm_message` | the model's private text and chain of thought - kept for debugging, displayed nowhere |
| `llm_delta` | streaming fragment of `llm_message`; nothing is rendered from it any more |
| `tool_call` | tool name + raw arguments |
| `tool_result` | the JSON returned by the tool |
| `say` | one accepted `speak` remark (also emitted by the controller for a human's typed line) |
| `nudge` | "you have not moved yet" reminder |
| `fallback_move` | the random move played for the model |
| `llm_error` | backend failure |
| `move` / `game_over` | emitted by the controller |
