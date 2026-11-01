# 持久化：games/ 与 logs/

对局 id = `game_yyyyMMdd_HHmmss`。它是一局所有产物的主文件名，也是你在网页
"Load saved game" 下拉框里选中的条目。

```
games/game_20260925_195646.pgn     每步之后重写
games/game_20260925_195646.json    完整状态 + 双方对话历史
logs/log_20260925_195646.log       追加写入，每行都带前缀
```

## PGN（`games/*.pgn`）

每步之后从头重写，因此文件始终是一份合法、完整的棋谱。头部字段：`Event`、
`Site`、`Date`、`Round`、`White`、`Black`、`WhiteKind`、`BlackKind`、
`WhiteModel`、`BlackModel`、`Result`、`Termination`、`PlyCount`（若棋局不是从
初始局面开始，还会加上 `FEN`/`SetUp`）。每一方在自己走子之前说过的话会作为着法
注释附上，由走这一着的那一方的对话历史推导：

```
1. e4 { Let me check the legal moves first. | I will play e4. } 1... e5 2. Nf3 1/2-1/2
```

只有该方**自己回合内**的发言才会落到这里。人类在对手思考时敲的那句话属于对话内容，
但不是对手那一步的注释。

## 对局 JSON（`games/*.json`）

```json
{
  "game_id": "game_20260925_195646",
  "created_at": "2026-09-25T19:56:46",
  "mode": "human-vs-llm",
  "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "white": {"kind": "human", "profile": "", "model": "", "label": ""},
  "black": {"kind": "llm", "profile": "openai", "model": "gpt-4o-mini", "label": ""},
  "moves_san": ["e4", "e5"],
  "moves_uci": ["e2e4", "e7e5"],
  "white_chat_history": [
    {"role": "user", "content": "That is a bold pawn.", "spoken": true, "seq": 1, "ply": 1}
  ],
  "black_chat_history": [
    {"role": "user", "content": "It is your turn. You play black. ..."},
    {"role": "assistant", "content": "", "reasoning": "If I take on e4 ...", "seq": 2, "ply": 2, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "speak", "arguments": "{\"text\": \"Hold my rook.\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "speak", "content": "{\n  \"ok\": true, ... }"}
  ],
  "result": {"winner": null, "reason": "repetition", "detail": "...", "pgn_result": "1/2-1/2"},
  "finished": true
}
```

规则：

- 对话历史是**完整**的，包括那些已经不再发给模型的部分。`user`、`assistant` 和
  `tool` 消息都会保存；系统提示不保存。
- 任何对阵下都会写 `white_chat_history` 与 `black_chat_history` —— LLM 互弈时
  两者都有内容。
- **发言不是独立字段。** 模型的发言是 assistant 消息上 `speak` 工具调用的 `text`
  参数；人类的发言是带 `"spoken": true` 的 `user` 消息。没有单独的 talk 列表，
  assistant 的 `content` 是永不展示的私有文本。
- 当后端返回思维链时（`reasoning_content`、`reasoning`、`thinking`、`think`），
  assistant 消息上会多一个 `reasoning` 字段，仅供排查。
- 发言旁边会写三个记账字段，并在历史发回后端前被剥除：`seq` 把双方发言放进同一条
  时间线（两份历史是各自独立的列表，对手思考时敲的那句话否则无处安放），`ply` 是
  该发言归属的、从 1 开始的 ply（PGN 注释用的就是它；`0` 表示终局之后说的，不属于
  任何一步，因此也不会成为注释），`spoken` 标记人类的 `user`
  消息。见 `src/agent/loop.py` 的 `_for_backend()`。
- 界面上的对话与 PGN 注释都由这两份历史推导，所以不会互相漂移：同一条发言同时喂给
  两者。
- 同时保留 `moves_uci` 与 `moves_san`，这样即使 SAN 有歧义也能重放着法。
- 在 `speak` 工具出现之前保存的对局没有任何发言，打开后对话为空；不会回退去展示它们
  的 `content`。

## 日志（`logs/*.log`）

每局由 `src/game/storage.py` 里的 `setup_logging()` 重新配置根 logger。
`MultilineFormatter` 会在多行记录的**每一行**上重复时间戳 / 级别 / logger 前缀，
因此 traceback 和 ASCII 棋盘都能保持对齐：

```
2026-09-25 19:56:46 | INFO  | src.game.controller      | Move 1: e4 (e2e4) played by white
2026-09-25 19:56:46 | INFO  | src.tools.executor       | Tool make_move failed (illegal_move): ...
2026-09-25 19:56:46 | ERROR | src.game.controller      | Traceback (most recent call last):
2026-09-25 19:56:46 | ERROR | src.game.controller      |   File "...", line 12, in run
2026-09-25 19:56:46 | ERROR | src.game.controller      | RuntimeError: boom
```

吵闹的第三方 logger（`httpx`、`httpcore`、`openai._base_client`）与 uvicorn 的
访问日志会被过滤掉。

## 续局

在网页界面里，从 "Load saved game" 选一局再点 Load（可选择性地覆盖任意一方）。
服务端读取 JSON，通过重放已存着法重建棋盘，恢复双方对话历史，并继续写入**同一个**
日志文件（`log_20260925_195646.log`）。如果对局 id 里解析不出时间戳，就改用当前
时间。进程退出前一定会保存对局（FastAPI 的 shutdown 钩子），所以 Ctrl-C 不会丢棋。
