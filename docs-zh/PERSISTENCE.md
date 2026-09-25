# 持久化：games/ 与 logs/

对局 id = `game_yyyyMMdd_HHmmss`。它是一局所有产物的主文件名，也是传给
`--load` 的值。

```
games/game_20260925_195646.pgn     每步之后重写
games/game_20260925_195646.json    完整状态 + 双方对话历史
logs/log_20260925_195646.log       追加写入，每行都带前缀
```

## PGN（`games/*.pgn`）

每步之后从头重写，因此文件始终是一份合法、完整的棋谱。头部字段：`Event`、
`Site`、`Date`、`Round`、`White`、`Black`、`WhiteKind`、`BlackKind`、
`WhiteModel`、`BlackModel`、`Result`、`Termination`、`PlyCount`（若棋局不是从
初始局面开始，还会加上 `FEN`/`SetUp`）。每个 Agent 在该 ply 的公开发言会作为着法
注释附上，例如：

```
1. e4 { Let me check the legal moves first. | I will play e4. } 1... e5 2. Nf3 1/2-1/2
```

## 对局 JSON（`games/*.json`）

```json
{
  "game_id": "game_20260925_195646",
  "created_at": "2026-09-25T19:56:46",
  "mode": "human-vs-llm",
  "start_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "white": {"kind": "human", "profile": "default", "model": "", "label": ""},
  "black": {"kind": "llm", "profile": "default", "model": "gpt-4o-mini", "label": ""},
  "moves_san": ["e4", "e5"],
  "moves_uci": ["e2e4", "e7e5"],
  "white_chat_history": [],
  "black_chat_history": [
    {"role": "user", "content": "It is your turn. You play black. ..."},
    {"role": "assistant", "content": "Checking my options.", "reasoning": "If I take on e4 ...", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_legal_moves", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "get_legal_moves", "content": "{\n  \"ok\": true, ... }"}
  ],
  "result": {"winner": null, "reason": "repetition", "detail": "...", "pgn_result": "1/2-1/2"},
  "finished": true,
  "seed": null
}
```

规则：

- 对话历史是**完整**的，包括那些已经不再发给模型的部分。`user`、`assistant` 和
  `tool` 消息都会保存；系统提示不保存。
- 任何对阵下都会写 `white_chat_history` 与 `black_chat_history` —— LLM 互弈时
  两者都有内容。
- 当后端返回思维链时（`reasoning_content`、`reasoning`、`thinking`、`think`），
  assistant 消息上会多一个 `reasoning` 字段，与 `content` 并列保存。发言只存在
  这一处，没有单独的 talk 列表。
- PGN 的着法注释和界面上的合并对话都由这些历史推导：一个 ply 映射到走它的那一方，
  以及该方的第 n 个回合（一个回合从每条 `user` 消息开始）。
- 同时保留 `moves_uci` 与 `moves_san`，这样即使 SAN 有歧义也能重放着法。

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

`python -m src.main --load game_20260925_195646` 读取 JSON，通过重放已存着法重建
棋盘，恢复双方对话历史，并继续写入**同一个**日志文件
（`log_20260925_195646.log`）。如果对局 id 里解析不出时间戳，就改用当前时间。
进程退出前一定会保存对局（`atexit` + FastAPI 的 shutdown 钩子），所以 Ctrl-C
不会丢棋。
