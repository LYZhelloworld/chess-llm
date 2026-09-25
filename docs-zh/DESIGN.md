# chess-llm 设计说明

## 1. 这个项目是什么

一个国际象棋对局平台，其中一方或双方可以是 LLM。LLM 从不直接修改棋盘：它只能
调用 JSON 工具（`get_legal_moves`、`make_move` 等），因此它走的每一步都由
`python-chess` 校验。

支持的对阵：

| 白方 | 黑方 | 怎么跑 |
| --- | --- | --- |
| human | llm | 默认：你在浏览器里点击，模型应答 |
| llm | llm | 服务端自动驱动双方 |
| human | human | 不花 token 就能测试棋盘 |
| llm | llm（无头） | `--headless`，完全不用浏览器 |

## 2. 分层架构

```
UI / 前端                    src/ui/server.py (FastAPI) + src/ui/static/index.html
        |  REST + SSE
对局服务 / 状态机             src/game/controller.py, src/game/state.py
        |
规则引擎                     src/engine/board.py, src/engine/rules.py (python-chess)
        |
LLM Agent 层                 src/agent/loop.py, src/agent/llm_client.py
        |
工具调用                     src/tools/executor.py, src/tools/schema.py
        |
持久化                       src/game/storage.py (games/, logs/)
```

依赖方向永远向下：Agent 层不 import FastAPI，UI 不直接碰 python-chess，除
Agent 循环外没有任何模块 import 提示词模块。

### 模块地图

| 路径 | 职责 |
| --- | --- |
| `src/main.py` | 命令行：创建或加载对局，启动 Web 界面或无头运行器 |
| `src/config.py` | `config.yaml` + 环境变量 + 命令行覆盖，配置数据类 |
| `src/prompts.py` | 项目中**所有**提示词字符串 |
| `src/engine/board.py` | `ChessEngine`：FEN、ASCII、合法着法、SAN/UCI 解析、推演 |
| `src/engine/rules.py` | 终局判定（将杀、逼和、和棋）与 `GameResult` |
| `src/engine/errors.py` | 返回给 LLM 的错误分类 |
| `src/tools/schema.py` | OpenAI function-calling 工具定义 |
| `src/tools/executor.py` | 执行一次工具调用，始终以 JSON 应答 |
| `src/agent/llm_client.py` | OpenAI 兼容客户端、重试、`AssistantMessage` |
| `src/agent/loop.py` | 回合循环、对话记忆与滑动上下文窗口 |
| `src/game/state.py` | 可序列化的 `GameState` / `PlayerSpec` |
| `src/game/controller.py` | 该谁走、落子后的记账、保存 |
| `src/game/storage.py` | PGN + JSON 写入、日志初始化、对局加载 |
| `src/ui/server.py` | REST 端点与 SSE 事件流 |
| `src/ui/static/index.html` | 单页棋盘界面（无需构建步骤） |

## 3. 一步棋的流程

1. UI（或无头运行器）向控制器询问轮到谁走。
2. **人类**：`POST /api/move`，提交 SAN 或 UCI 字符串 -> `submit_human_move`。
   **LLM**：`run_llm_turn()` 启动 Agent 循环（见 `LLM-LOOP.md`）。
3. 着法落定：追加到 `moves_san` / `moves_uci`，记日志，并作为 `move` 事件推送
   到 SSE 流。
4. `evaluate_position()` 检查将杀、逼和、子力不足、重复局面、无进展规则与步数
   上限。
5. 若对局继续，重写 PGN 与 JSON；若已结束，保存结果、写入文件并发出
   `game_over` 事件。
6. 若下一方是 LLM，服务端调度下一个回合。

## 4. 值得知道的决策

- **只支持 OpenAI 兼容接口。** 一条代码路径（`chat.completions` + `tools`）覆盖
  OpenAI、DeepSeek、Moonshot、vLLM、Ollama 以及任何说这个协议的后端；换供应商
  只是换一个 `config.yaml` profile。
- **桌上发言仅供展示。** 模型以普通文本写出的内容会显示在 Web 界面、存进对局
  JSON 和 PGN 注释，但**不会**注入对手的上下文。这样对手对提示词注入免疫，上下
  文也保持精简。想反过来就改 `LLM-LOOP.md`。
- **思维链是提示词层面的。** 系统提示要求模型私下推理、公开文本简短；不发送任何
  厂商专属的 `reasoning_effort` 参数，因此同一套代码到处都能用。
- **`try_make_move` 是沙盒。** 它在棋盘的副本上（或在调用方提供的 FEN 上）演算，
  绝不改动正在进行的对局。
- **`make_move` 很严格。** 只接受 `get_legal_moves` 返回的字符串，每次一步，调用
  即结束回合。
- **失败绝不致命。** 工具报错会被分类后交回模型；后端故障则退化为随机走一步合法
  着法（带种子，便于复现），而不是让对局崩掉。

## 5. Web 界面

- `GET /` - 单页应用。
- `GET /api/state` - 完整快照：FEN、ASCII、合法着法、着法列表、双方对话历史、
  界面展示用的合并 `conversation`、结果。
- `POST /api/move` - `{ "move": "Nf3" }`（SAN 或 UCI）。
- `POST /api/resign` - `{ "side": "white" }`。
- `POST /api/new` - 开新局，逐边选择 human/llm。
- `POST /api/load` - `{ "game_id": "game_20260925_195646" }`。
- `GET /api/games` - 已保存的对局 id。
- `GET /api/events` - Server-Sent Events（`turn_start`、`tool_call`、
  `tool_result`、`llm_message`、`move`、`game_over` 等）。UI 每收到一个事件就
  重新拉取状态。

LLM 回合在 worker 线程里跑（`asyncio.to_thread`），这样事件循环仍能响应请求；
并用一把锁保证同一时刻只有一个回合在跑。

## 6. 配置

`config.yaml` 保存 LLM profile（base URL、`api_key`、`api_key_env` 兜底、模型、
temperature、超时、重试、以及 `reasoning` 模式）、和棋阈值、循环上限与存储目录。
它是**必需的**：仓库
只提供 `config.example.yaml`，`config.yaml` 缺失时 `src/config.py` 会抛
`ConfigError`（命令行打印复制指令并以退出码 2 结束）。`config.yaml` 被 git 忽略，
因为里面有 API 密钥。

密钥的优先级：profile 里的 `api_key` → `api_key_env` 指定的环境变量 →
`--api-key`。`OPENAI_BASE_URL` / `OPENAI_MODEL` 会覆盖默认 profile，命令行参数
覆盖一切。参数列表见 `README_zh.md`。

不过它只在**至少一方是 LLM** 时才必需：`src.main` 用 `required=False` 加载配置，
等 `GameState` 建好之后，若 `config.yaml` 缺失且有一方是 LLM 才报错。因此
`human-vs-human` 完全可以在没有配置文件的情况下启动（只打印一行提示），这样在
还没有任何可用端点时棋盘依然能用。

## 6.1 对局模式

`--mode` 是 `--white` / `--black` 的快捷写法（显式传入后者时以后者为准）：

| 模式 | 白方 | 黑方 | 需要配置 |
| --- | --- | --- | --- |
| `human-vs-llm`（默认） | human | LLM | 是 |
| `llm-vs-human` | LLM | human | 是 |
| `llm-vs-llm` | LLM | LLM | 是 |
| `human-vs-human` | human | human | 否 |

`human-vs-human` 是热座模式：可以行动的座位就是当前行棋方（界面问控制器，为错误
一方提交的着法会被拒绝）。`--headless` 仍然要求双方都是 LLM。

## 7. 相关文档

- [`TOOLS.md`](TOOLS.md) - 每个工具的确切 JSON 契约。
- [`LLM-LOOP.md`](LLM-LOOP.md) - Agent 循环、催促、上下文窗口、兜底。
- [`PERSISTENCE.md`](PERSISTENCE.md) - PGN、JSON 与日志格式。
