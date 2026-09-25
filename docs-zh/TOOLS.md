# 工具契约

每个工具都以 JSON 接收参数、以 JSON 字符串返回。定义位于 `src/tools/schema.py`
（OpenAI function-calling 格式），执行位于 `src/tools/executor.py`。

## 错误模型

失败的调用绝不会把异常抛进 Agent 循环，而是返回：

```json
{
  "ok": false,
  "tool": "make_move",
  "error": {
    "category": "illegal_move",
    "message": "Illegal move: 'Nf5' is not one of the 20 legal moves in the current position.",
    "hint": "Call get_legal_moves and pick one of the returned moves."
  },
  "message": "Illegal move: 'Nf5' ... Call get_legal_moves and pick one of the returned moves."
}
```

| 分类 | 含义 |
| --- | --- |
| `invalid_input` | 参数不是合法 JSON、类型不对，或 SAN/FEN 字符串无法解析 |
| `illegal_move` | 记谱格式正确，但在当前局面下这一着不合法 |
| `illegal_state` | 当前不允许这个动作（例如回合已经落定之后还要走子） |
| `engine_error` | python-chess 以意外方式拒绝了请求 |
| `unknown_tool` | 模型自己编了个工具名 |

## get_board

参数：无。

```json
{
  "ok": true,
  "tool": "get_board",
  "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "side_to_move": "white",
  "your_side": "white",
  "check_status": "no check",
  "castling_rights": "KQkq",
  "en_passant": "-",
  "halfmove_clock": 0,
  "fullmove_number": 1,
  "last_move": null,
  "legal_moves_count": 20,
  "game_over": false,
  "result": null
}
```

`check_status` 取值为 `no check`、`white is in check`、`black is in check`
三者之一。

## get_board_ascii

参数：无。返回 `fen`、`side_to_move` 和 `ascii`：

```
r n b q k b n r
p p p p p p p p
. . . . . . . .
. . . . . . . .
. . . . . . . .
. . . . . . . .
P P P P P P P P
R N B Q K B N R
```

## get_legal_moves

参数：无。只返回 SAN 字符串：

```json
{"ok": true, "tool": "get_legal_moves", "fen": "...", "side_to_move": "white",
 "count": 20, "moves": ["a3", "b3", "...", "Nf3"], "note": "make_move only accepts one of these exact SAN strings."}
```

## try_make_move

```json
{"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
 "moves": ["e4", "e5", "Nf3"]}
```

- `fen` 可选；省略时使用当前棋盘。
- `moves` 至少需要一步 SAN，且必须黑白交替：若第一步属于白方，第二步就必须属于
  黑方，依此类推。这条由 python-chess 强制，违反时会连同出错的步骤序号一起报告。
- 不会落子，真实棋盘不受影响。

```json
{
  "ok": true,
  "tool": "try_make_move",
  "simulated": true,
  "committed": false,
  "start_fen": "... w KQkq - 0 1",
  "resulting_fen": "... b KQkq - 1 2",
  "side_to_move": "black",
  "moves_requested": 3,
  "moves_applied": 3,
  "steps": [{"index": 0, "san": "e4", "uci": "e2e4", "side": "white", "ok": true}],
  "ascii": "...",
  "is_check": false,
  "is_checkmate": false,
  "legal_moves_count": 29,
  "message": "All 3 move(s) are legal. Nothing was committed to the real game; call make_move with the first move to play it."
}
```

失败时 `ok` 为 `false`，`steps` 显示这条线走到第几步，`message` 会先解释交替规则
再给出引擎错误。

## make_move

```json
{"move": "Nf3"}
```

- 只接受一步，且必须是 `get_legal_moves` 返回过的字符串。
- 成功后这一着落在真实棋盘上，回合结束。

```json
{
  "ok": true,
  "tool": "make_move",
  "committed": true,
  "move": "Nf3",
  "uci": "g1f3",
  "played_by": "white",
  "fen": "...",
  "side_to_move": "black",
  "check_status": "no check",
  "is_check": false,
  "is_checkmate": false,
  "legal_moves_count": 20,
  "game_over": false,
  "result": null,
  "message": "Played Nf3. Your turn is over."
}
```

如果一条 assistant 消息里含多个 `make_move` 调用，只有第一个生效，其余以
`illegal_state` 应答，以满足 API 的要求。

## resign

```json
{"reason": "down a queen with no compensation"}
```

```json
{"ok": true, "tool": "resign", "committed": true, "resigned_by": "black",
 "reason": "down a queen with no compensation",
 "message": "Black resigned. The game is over."}
```
