# The LLM turn loop

Implemented in `src/agent/loop.py` (`run_llm_turn`). One call = one move.

## 1. Prompting

Messages sent to the backend for every exchange:

```
[ system  ] system_prompt(side)                      <- src/prompts.py
[ ...     ] memory.window(loop.context_turns)        <- sliding window of history
```

The system prompt covers: the side being played, that public text is shown to
the audience while private reasoning is not, the tool workflow, the hard rules
(one move per `make_move`, only SAN from `get_legal_moves`, read
`error.category` on failure), and the fact that a random move is played if the
turn budget runs out.

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
```

The legal move list is deliberately *not* included: the model has to ask
`get_legal_moves`, which keeps the tool contract as the only source of truth.

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
   agent (`loop.fallback_random_move`), logged as `[random fallback]`, and a
   user message telling the model what happened is appended so the transcript
   stays coherent.
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

Everything the assistant writes as normal text is:

- emitted as an `llm_message` event,
- kept in the chat history of that side (nothing else is stored: no separate
  talk list),
- written into the PGN as a move comment, derived from that history.

Chain of thought is captured the same way. If the backend returns it in
`reasoning_content` (or `reasoning` / `thinking` / `think`), it is stored on
the assistant message under `reasoning` and shown in the browser behind a
toggle. The profile field `reasoning` decides whether it is sent back with the
history: `auto` (default) sends it and stops if the backend refuses the request,
`on` always sends it, `off` never sends it. In `auto` mode a refused request is
retried once without the chain of thought, and only if that also fails does the
turn fall back to a random move as usual.

Public text is **not** injected into the opponent's context. To change that,
add a step in `GameController.run_llm_turn` that prepends the opponent's last
remark to the next turn prompt.

## 5. Events emitted

| Type | Payload |
| --- | --- |
| `user_prompt` | the turn prompt sent to the model |
| `llm_message` | public text written by the model, plus its chain of thought |
| `tool_call` | tool name + raw arguments |
| `tool_result` | the JSON returned by the tool |
| `nudge` | "you have not moved yet" reminder |
| `fallback_move` | the random move played for the model |
| `llm_error` | backend failure |
| `talk` / `move` / `game_over` | emitted by the controller |
