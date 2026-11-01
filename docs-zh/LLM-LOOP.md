# LLM 回合循环

实现位于 `src/agent/loop.py`（`run_llm_turn`）。一次调用 = 一步棋。

## 1. 提示

每次与后端交互时发送的消息：

```
[ system  ] system_prompt(side)                      <- src/prompts.py
[ ...     ] memory.window(loop.context_turns)        <- 历史记录的滑动窗口
```

系统提示涵盖：所执的一方、普通文本是私有的而 `speak` 才是公开的、工具用法、硬性规则
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

Material balance (P=1, N=3, B=3, R=5, Q=9; king excluded): White 39 - Black 39 (material is level); you are level.

Legal moves (SAN - `make_move` accepts exactly one of these):
a3
b3
...
e4
...

Opponent has not spoken yet this game.
```

合法着法列表和每一回合的子力对比都**内联**给出，模型通常不需要再调
`get_legal_moves`；但当列表过期或需要核对时，该工具（以及 `get_board` /
`get_board_ascii`）依然可用。对手自本方上次行动以来的发言也会附上，方便 Agent 接话。

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
3. 交互预算耗尽 -> 为该 Agent **随机走一步合法着法**，记为 `[random fallback]`，
   并追加一条 user 消息说明发生了什么，保证对话记录连贯。一个回合必须改变棋盘：
   不落子就结束的话，轮到谁走棋没变，驱动层会立刻再跑同一方。
4. 重试后后端仍失败 -> 同样的随机走子兜底，错误记录在 `TurnOutcome.error` 里。

## 3. 上下文窗口

- **完整**历史保存在 `ChatMemory` 中并持久化到对局 JSON
  （`white_chat_history` / `black_chat_history`），包含 user、assistant 和 tool
  消息（绝不包含系统提示）。
- 只发送最近 `loop.context_turns`（默认 20）轮。一个*轮* = 一条 `user` 消息加上
  其后直到下一条 `user` 消息之前的所有内容，这保证 `tool_calls` 块永远不会与它的
  `tool` 结果分离。开头的孤儿 tool 消息会作为兜底被丢弃。

## 4. 桌上发言

模型的发言只有通过 `speak` 工具才能到达任何人。它写下的普通文本——包括思维链——都是
私有的：留在对话记录里供排查和模型自己回忆，不展示在任何地方。

每一次被接受的 `speak` 调用会：

- 作为 `say` 事件发出，
- 作为一条普通的 assistant `tool_calls` 消息写进该方的对话历史，因此不需要额外存储，
- 当该方走出它之前的那一步时，成为那一着的 PGN 注释。

思维链从 `reasoning_content`（或 `reasoning` / `thinking` / `think`）中捕获，存到
assistant 消息的 `reasoning` 字段。profile 的 `reasoning` 字段决定它是否随历史一起
回传：`auto`（默认）会回传，一旦后端拒绝请求就不再回传；`on` 始终回传；`off` 从不
回传。`auto` 模式下被拒绝的请求会不带思维链重试一次，重试也失败才按常规退化为随机
走子。思维链永不展示。

发言**会**注入对手的上下文。`GameController` 会收集对手自本方上次行动以来说过的一切
——对手的 `speak` 调用，或操作者敲进去的话——并以
`Opponent's remarks since your last turn:` 的形式放进回合提示。每条发言只投递一次：
构造提示时水位就会推进，因此重复的一句不会在之后每个回合反复出现。对手一直没说话时
显示 "Opponent has not spoken yet this game."。

人类随时可以输入，包括对手正在思考的时候。发言会立刻存储并显示，但会在对手**下一个**
回合开始时才进入提示——已经跑起来的回合早已构造好提示了。

## 5. 发出的事件

| 类型 | 载荷 |
| --- | --- |
| `user_prompt` | 发给模型的回合提示 |
| `llm_message` | 模型的私有文本与思维链——仅供排查，不展示 |
| `llm_delta` | `llm_message` 的流式片段；界面不再据此渲染任何内容 |
| `tool_call` | 工具名 + 原始参数 |
| `tool_result` | 工具返回的 JSON |
| `say` | 一条被接受的 `speak` 发言（人类输入的那句也由控制器发出） |
| `nudge` | "你还没走子"提醒 |
| `fallback_move` | 替模型随机走的那一步 |
| `llm_error` | 后端故障 |
| `move` / `game_over` | 由控制器发出 |
