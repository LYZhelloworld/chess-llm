# chess-llm

[English](README.md) | 简体中文

和 LLM 下国际象棋，或者让两个 LLM 互相下。模型不能直接改写棋盘：它只能调用 JSON
工具（`get_board`、`get_board_ascii`、`get_legal_moves`、`make_move`、`resign` 等），每一步都由
[python-chess](https://python-chess.readthedocs.io/) 校验。

```
UI / 前端                FastAPI + 单页 HTML 棋盘
        |
对局服务 / 状态机
        |
规则引擎                python-chess
        |
LLM Agent 层            OpenAI 兼容的工具调用
        |
工具                    get_board / get_board_ascii / get_legal_moves / try_make_move / make_move / resign
```

## 安装

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml                # 然后把你的 API key 填进去
```

需要 Python 3.10+。即使没有 `config.yaml` 服务也能启动 —— 你可以直接开起来、不花
token 玩 human vs human。只有在开一个带 LLM 一方的对局时才需要 `config.yaml`；它
被 git 忽略，因为保存着你的 API 密钥。

## 运行

```bash
python -m src.main
# 打开 http://127.0.0.1:8000
```

然后在浏览器里：

- **New game** —— 选择白方、黑方各由谁执子。每一方要么是 `human`，要么是
  `config.yaml` 里的一个 LLM profile；LLM 一方需要该 profile 配了 API key。
- **Load saved game** —— 选择一局之前的棋；可选择性地覆盖任意一方（例如换成另一个
  profile，或自己接管某一方）。
- 轮到人类时点击棋子（或输入 SAN）走子，并实时看双方合并后的对话逐字流出。每一手
  的思维链显示在一个可折叠块里（模型仍在思考时展开，开始回复后折叠；历史对局默认折叠）。

`human-vs-human` 是热座模式：两名玩家共用棋盘，轮到谁走谁就能走，投降按钮作用于
当前行棋方。

Web 界面可以点击棋子（或输入 SAN）、在一条合并的时间线里看双方 Agent 说了什么
（提示词、工具调用与返回结果不显示；思维链以可折叠块展示）、
开新局、加载已保存的对局。

## 命令行参数

命令行只携带服务端需要的内容。对阵本身在浏览器里选。

| 参数 | 含义 |
| --- | --- |
| `--config` | 指向 `config.yaml`（由 `config.example.yaml` 复制而来） |
| `--port` | Web 服务端口（默认 8000；监听地址固定为 127.0.0.1） |
| `--log-level` | DEBUG / INFO / WARNING / ERROR |

## 配置

模型在 `config.yaml` 里配置，环境变量可作为默认 profile 的可选覆盖。

### 1. `config.yaml`（推荐）

复制模板创建一次即可：

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
```

`api_key` 直接写密钥，优先级最高。如果你不想把密钥写进文件，就留空
`api_key: ""` 并设 `api_key_env: OPENAI_API_KEY` —— 那只是要读取的**环境变量名**。

`reasoning` 控制推理模型返回的思维链（`reasoning_content`、`reasoning` 等）。它
始终会存进对局 JSON，并在界面上以可折叠块展示；这个模式决定它是否也随历史
回传给模型：`auto`（默认）先试，后端拒绝就不再回传；`on` 始终回传；`off` 从不回传。

在界面开新局时，从下拉框为每一方选一个 profile —— 不再有等价的命令行参数。

HTTP API 无条件只接受本机请求。它没有鉴权，而且每个 LLM 回合都会花掉你的额度，
因此只监听 `127.0.0.1` —— 没有 `--host` 参数，也没有配置项可以改变这一点。到达服务的
请求还会校验 `Host` 头，这一层挡的是浏览器里已打开的页面通过 DNS rebinding 打到本机端口。

### 2. 环境变量（可选）

只有 `api_key` 为空的 profile 会用到。此外 `OPENAI_BASE_URL` 与 `OPENAI_MODEL`
会覆盖默认 profile 的端点和模型：

```bash
export OPENAI_API_KEY=sk-...     # 由 "default" profile 通过 api_key_env 读取
export OPENAI_BASE_URL=...       # 可选，覆盖 base_url
export OPENAI_MODEL=gpt-4o       # 可选，覆盖 model
```

任何 OpenAI 兼容端点都能用（OpenAI、DeepSeek、Moonshot、vLLM、Ollama、
LM Studio 等），因为只用到 `chat.completions` + function calling。

和棋判定：三次重复局面、50 个完整回合无吃子无兵进、子力不足、逼和，以及 200
个完整回合的硬性上限。

## 一次 Agent 回合是怎么跑的

1. Agent 收到自己执子方、FEN、易位权、将军状态、计数器和 ASCII 棋盘。
2. 它可以发言（公开，界面可见）并调用工具。它的私有思维链不会展示。
3. 如果还没走出合法的一步，会被提示使用 `make_move`。
4. 20 轮对话后仍未走子，则替它随机走一步合法着法。

详情见 [`docs-zh/LLM-LOOP.md`](docs-zh/LLM-LOOP.md)。

## 产生的文件

```
games/game_yyyyMMdd_HHmmss.pgn    每步之后重写，Agent 的发言作为着法注释
games/game_yyyyMMdd_HHmmss.json   模式、着法、结果，以及双方完整对话历史
logs/log_yyyyMMdd_HHmmss.log      每一行都带时间戳前缀
```

## 测试

```bash
python -m pytest -q
```

## 文档

- [`docs-zh/`](docs-zh/README.md) - 中文文档索引
- [`docs-zh/DESIGN.md`](docs-zh/DESIGN.md) - 架构、模块地图、关键决策
- [`docs-zh/TOOLS.md`](docs-zh/TOOLS.md) - 每个工具的 JSON 契约
- [`docs-zh/LLM-LOOP.md`](docs-zh/LLM-LOOP.md) - Agent 循环、提示、上下文窗口
- [`docs-zh/PERSISTENCE.md`](docs-zh/PERSISTENCE.md) - PGN / JSON / 日志格式
- [`AGENTS.md`](AGENTS.md) - 本仓库的协作约定（英文）
