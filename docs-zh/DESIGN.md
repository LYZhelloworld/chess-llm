# chess-llm 设计说明

## 1. 这个项目是什么

一个国际象棋对局平台，其中一方或双方可以是 LLM。LLM 从不直接修改棋盘：它只能
调用 JSON 工具（`get_legal_moves`、`make_move` 等），因此它走的每一步都由
`python-chess` 校验。

支持的对阵：

| 白方 | 黑方 | 怎么跑 |
| --- | --- | --- |
| human | llm | 你在浏览器里点击，模型应答 |
| llm | llm | 服务端自动驱动双方 |
| human | human | 不花 token 就能测试棋盘 |

所有座位都在开新对局（或读取旧对局）时通过网页界面选择。命令行没有设置对阵
的参数——服务端启动后只显示空棋盘，等你来选。

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
- **桌上发言会注入上下文。** 模型以普通文本写出的内容会显示在 Web 界面、存进对局
  JSON 和 PGN 注释，并作为 `Opponent's last spoken remark` 转发给对手，用于对手下一手
  开局。这样 Agent 可以接话回击，代价是会受到对方提示词注入的影响。详见 `LLM-LOOP.md`。
- **思维链是提示词层面的。** 系统提示要求模型私下推理、公开文本简短；不发送任何
  厂商专属的 `reasoning_effort` 参数，因此同一套代码到处都能用。
- **`try_make_move` 是沙盒。** 它在棋盘的副本上（或在调用方提供的 FEN 上）演算，
  绝不改动正在进行的对局。
- **`make_move` 很严格。** 只接受 `get_legal_moves` 返回的字符串，每次一步，调用
  即结束回合。
- **失败绝不致命。** 工具报错会被分类后交回模型；后端故障则退化为随机走一步合法
  着法（带种子，便于复现），而不是让对局崩掉。
- **HTTP API 只接受本机请求，且没有开关可以放开。** 监听地址是一个常量（`src/main.py`
  的 `BIND_HOST`），既不是命令行参数也不是配置项，所以端口在网络层面就不可达 —— 绑
  `0.0.0.0` 之后再拒绝请求，端口对扫描器依然是可见的，而一个能这么做的开关本身就是陷阱。
  另有 `local_only` 中间件校验 `Host` 头，不匹配就返回 `403`，挡的是浏览器里已打开的
  页面通过 DNS rebinding 打到本机端口；这类请求的对端地址就是回环，所以按对端地址判断
  没有意义。这里没有鉴权，且每个 LLM 回合都在花运营者的额度。
- **通过 API 认输是人的操作。** `POST /api/resign` 只接受人类座位（API 调用方不能替
  模型认输），热座模式下只有当前行棋方可以；这条规则由控制器执行，而不只靠浏览器。
  模型自己认输走的是它自己的 `resign` 工具：该调用结束本回合，随后由控制器按这一回合
  的结果结算。

## 5. Web 界面

- `GET /` - 单页应用。
- `GET /api/state` - 完整快照：FEN、ASCII、合法着法、着法列表、双方对话历史、
  界面展示用的合并 `conversation`、结果。
- `POST /api/move` - `{ "move": "Nf3" }`（SAN 或 UCI）。
- `POST /api/resign` - `{ "side": "white", "reason": "..." }`（reason 可选）。
  除非该座位是人类（热座模式下还必须是当前行棋方），否则返回 `400`。
- `POST /api/new` - 开新局，逐边选择 human/llm。
- `POST /api/load` - `{ "game_id": "game_20260925_195646" }`。
- `GET /api/games` - 已保存的对局 id。
- `GET /api/events` - Server-Sent Events（`turn_start`、`tool_call`、
  `tool_result`、`llm_message`、`move`、`game_over` 等）。UI 每收到一个事件就
  重新拉取状态。

LLM 回合在 worker 线程里跑（`asyncio.to_thread`），这样事件循环仍能响应请求；
并用一把锁保证同一时刻只有一个回合在跑。由于一个回合可能比它所属的对局活得更久，
每个控制器的事件都会经过一层守卫：一旦有新对局顶替它，它的事件就会被丢弃。

命令行只控制 `--port`，监听地址固定为回环。`local_only` 中间件
（`src/ui/server.py`）另外会拒绝 `Host` 头不是本机名字的请求，因此直接用
`create_app` 组装的场景也在覆盖范围内。

棋盘默认以白方视角绘制。当唯一的人类座位是黑方时会自动翻转，让操作者实际执的一方
在下方；热座模式因为两人共用一块屏幕，仍然保持白方在下。

## 6. 配置

`config.yaml` 保存 LLM profile（base URL、`api_key`、`api_key_env` 兜底、模型、
temperature、超时、重试、以及 `reasoning` 模式与 `stream` 模式）、和棋阈值、循环上限、
存储目录与 `server` 访问策略。仓库只提供 `config.example.yaml`，复制为 `config.yaml`
即可使用。`config.yaml` 被 git 忽略，因为里面有 API 密钥。

各配置段在加载时都会校验：结构写错（例如把 `llm_profiles` 写成字符串而不是映射）会抛出
指明出错键名的 `ConfigError`，而不是等到后面才冒出一个 `AttributeError`。

服务端**没有** `config.yaml` 也能启动：你可以直接开起来、不花 token 玩
human vs human。只有在开一个**带 LLM 的一方**的对局时才需要 profile——你在网页
"New game" 面板里选它，而且该 profile 必须配了 API key。若在 LLM 对局里选了没有
key（或不存在）的 profile，会被明确拒绝，而不是崩溃。

密钥的优先级：profile 里的 `api_key` → `api_key_env` 指定的环境变量。
`OPENAI_BASE_URL` / `OPENAI_MODEL` 环境变量会覆盖*默认* profile。命令行不再有
key、model 或座位相关的参数——这些都在浏览器里选。

## 6.1 对局模式

上面的四种对阵不变，但座位是在界面里选的，而不是命令行。`New game` 面板允许你
把白方、黑方各自设为 `human` 或 `llm:<profile>`（`<profile>` 是 `config.yaml` 里的
名字）。`Load` 会重放已存对局，并可选择性地把任意一方覆盖成另一个
`human` / `llm:<profile>`；若某一方保留 "(keep stored)"，但其存档 profile 已不可
用，则读取会被拒绝，必须先指定由谁来下这一方。

`human-vs-human` 是热座模式：可以行动的座位就是当前行棋方（界面问控制器，为错误
一方提交的着法会被拒绝）。

## 7. 相关文档

- [`TOOLS.md`](TOOLS.md) - 每个工具的确切 JSON 契约。
- [`LLM-LOOP.md`](LLM-LOOP.md) - Agent 循环、催促、上下文窗口、兜底。
- [`PERSISTENCE.md`](PERSISTENCE.md) - PGN、JSON 与日志格式。
