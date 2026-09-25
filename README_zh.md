# chess-llm

[English](README.md) | 简体中文

和 LLM 下国际象棋，或者让两个 LLM 互相下。模型不能直接改写棋盘：它只能调用 JSON
工具（`get_legal_moves`、`make_move`、`resign` 等），每一步都由
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
工具                    get_board / get_legal_moves / try_make_move / make_move / resign
```

## 安装

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml                # 然后把你的 API key 填进去
```

需要 Python 3.10+。只要有一方是 LLM，缺少 `config.yaml` 时就拒绝启动 ——
这个文件被 git 忽略，因为它保存着你的 API 密钥。`--mode human-vs-human`
是唯一的例外：它完全不需要配置文件，在你还在搭建（或下载）模型的时候很好用。

## 运行

```bash
# 人类（白）对 LLM（黑）- 打开 http://127.0.0.1:8000
python -m src.main

# 浏览器里看 LLM 互弈
python -m src.main --mode llm-vs-llm

# 不开浏览器的 LLM 互弈（每步打印棋盘，结束时打印结果）
python -m src.main --mode llm-vs-llm --headless

# 同屏双人（热座）：不需要模型，也不需要 config.yaml
python -m src.main --mode human-vs-human

# 续上一局
python -m src.main --load game_20260925_195646
```

`--mode` 只是两边的快捷写法；显式传 `--white` / `--black` 时以后者为准。
在 `human-vs-human` 下两名玩家共用一块棋盘：轮到谁走谁就能走，投降按钮作用于
当前行棋方。

Web 界面可以点击棋子（或输入 SAN）、在一条合并的时间线里看双方 Agent 说了什么
（提示词、工具调用与返回结果不显示；思维链由「show chain of thought」开关控制）、
开新局、加载已保存的对局。

## 命令行参数

| 参数 | 含义 |
| --- | --- |
| `--mode` | `human-vs-llm`、`llm-vs-human`、`llm-vs-llm`、`human-vs-human` |
| `--white` / `--black` | `human` 或 `llm`（默认 human / llm；优先级高于 `--mode`） |
| `--white-profile` / `--black-profile` | `config.yaml` 里命名好的 profile |
| `--model`、`--white-model`、`--black-model` | 覆盖模型 |
| `--base-url` | OpenAI 兼容端点（DeepSeek、Moonshot、vLLM、Ollama 等） |
| `--api-key` | 覆盖两边的 API key |
| `--api-key-env` | 存放 API key 的环境变量名 |
| `--config` | 指向另一个 `config.yaml` |
| `--load` | 续 `game_yyyyMMdd_HHmmss` 这一局 |
| `--seed` | 随机兜底走子的随机种子 |
| `--headless` | 不启 Web 界面（两边都必须是 LLM） |
| `--host` / `--port` | Web 服务监听地址（默认 127.0.0.1:8000） |
| `--log-level` | DEBUG / INFO / WARNING / ERROR |

## 配置

配置模型有三种方式，按此顺序生效（后者覆盖前者）：
`config.yaml` → 环境变量 → 命令行参数。

### 1. `config.yaml`（推荐）

复制模板创建一次即可：

```bash
cp config.example.yaml config.yaml
```

```yaml
default_profile: default          # 未指定 --*-profile 时使用

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
始终会存进对局 JSON，并在界面上由一个开关控制是否显示；这个模式决定它是否也随历史
回传给模型：`auto`（默认）先试，后端拒绝就不再回传；`on` 始终回传；`off` 从不回传。

两边用不同 profile：

```bash
python -m src.main --white-profile deepseek --black-profile local
```

### 2. 环境变量（可选）

只有 `api_key` 为空的 profile 会用到。此外 `OPENAI_BASE_URL` 与 `OPENAI_MODEL`
会覆盖默认 profile 的端点和模型：

```bash
export OPENAI_API_KEY=sk-...     # 由 "default" profile 通过 api_key_env 读取
export OPENAI_BASE_URL=...       # 可选，覆盖 base_url
export OPENAI_MODEL=gpt-4o       # 可选，覆盖 model
```

### 3. 命令行参数（临时实验最快）

```bash
python -m src.main --model gpt-4o                       # 两边同一个模型
python -m src.main --api-key sk-...                     # 两边同一个 key
python -m src.main --base-url http://127.0.0.1:11434/v1 --model qwen2.5:14b --api-key sk-no-key
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
