# LLM 回合循环

实现位于 `src/agent/loop.py`（`run_llm_turn`）。一次调用 = 一步棋。

## 1. 提示

每次与后端交互时发送的消息：

```
[ system  ] system_prompt(side)                      <- src/prompts.py
[ ...     ] memory.window(loop.context_turns)        <- 历史记录的滑动窗口
```

系统提示涵盖：所执的一方、公开文本会展示给观众而私有推理不会、工具用法、硬性规则
（`make_move` 每次一步、只接受 `get_legal_moves` 返回的 SAN、失败时读
`error.category`），以及回合预算耗尽会随机走一步。

回合开始时追加一条 `user` 消息（`turn_prompt`）：

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

这里**故意不**附上合法着法列表：模型必须自己去调 `get_legal_moves`，从而让工具
契约成为唯一的权威来源。

## 2. 循环

```
for exchange in 1 .. loop.max_tool_turns（默认 20）:
    response = client.chat(system + window, tools)
    保存 assistant 消息（content + tool_calls）

    if 响应里没有工具调用:
        追加催促语 "You have not played a legal move yet. Call
        make_move now with exactly one SAN move ..."
        continue

    for 每个工具调用:
        result = executor.execute(name, arguments)     # 始终是 JSON
        追加一条 role="tool" 消息，内容为该 JSON

    if 执行器达到了终结动作（make_move 或 resign）:
        停下来并汇报结果
```

退出条件：

1. `make_move` 成功 -> 这一着由控制器落定。
2. `resign` -> 对局以认输结果结束。
3. 交互预算耗尽 -> 为该 Agent **随机走一步合法着法**
   （`loop.fallback_random_move`），记为 `[random fallback]`，并追加一条 user
   消息说明发生了什么，保证对话记录连贯。
4. 重试后后端仍失败 -> 同样的随机走子兜底，错误记录在 `TurnOutcome.error` 里。

## 3. 上下文窗口

- **完整**历史保存在 `ChatMemory` 中并持久化到对局 JSON
  （`white_chat_history` / `black_chat_history`），包含 user、assistant 和 tool
  消息（绝不包含系统提示）。
- 只发送最近 `loop.context_turns`（默认 20）轮。一个*轮* = 一条 `user` 消息加上
  其后直到下一条 `user` 消息之前的所有内容，这保证 `tool_calls` 块永远不会与它的
  `tool` 结果分离。开头的孤儿 tool 消息会作为兜底被丢弃。

## 4. 桌上发言

assistant 以普通文本写下的所有内容会：

- 作为 `llm_message` 事件发出，
- 保存在该方的对话历史里（不另外存任何东西：没有单独的 talk 列表），
- 作为着法注释写进 PGN —— 由这份历史推导得出。

思维链同样会被捕获。如果后端把它放在 `reasoning_content`（或 `reasoning` /
`thinking` / `think`）里，就会存到 assistant 消息的 `reasoning` 字段，并在界面上
用一个开关控制是否显示。profile 的 `reasoning` 字段决定它是否随历史一起回传：
`auto`（默认）会回传，一旦后端拒绝请求就不再回传；`on` 始终回传；`off` 从不回传。
`auto` 模式下被拒绝的请求会不带思维链重试一次，重试也失败才按常规退化为随机走子。

公开文本**不会**注入对手的上下文。要改变这一点，在
`GameController.run_llm_turn` 里加一步：把对手最近一次发言前置到下一回合的提示中。

## 5. 发出的事件

| 类型 | 载荷 |
| --- | --- |
| `user_prompt` | 发给模型的回合提示 |
| `llm_message` | 模型写下的公开文本，以及它的思维链 |
| `tool_call` | 工具名 + 原始参数 |
| `tool_result` | 工具返回的 JSON |
| `nudge` | "你还没走子"提醒 |
| `fallback_move` | 替模型随机走的那一步 |
| `llm_error` | 后端故障 |
| `talk` / `move` / `game_over` | 由控制器发出 |
