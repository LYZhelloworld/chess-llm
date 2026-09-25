# docs-zh

chess-llm 的简体中文文档。从这里开始，按主题挑选。

| 文档 | 内容 |
| --- | --- |
| [`DESIGN.md`](DESIGN.md) | 架构、分层图、模块地图、对局模式、配置 |
| [`TOOLS.md`](TOOLS.md) | LLM 可调用的每个工具的 JSON 契约 |
| [`LLM-LOOP.md`](LLM-LOOP.md) | 一次 Agent 回合如何运行：提示词、催促、上下文窗口、兜底 |
| [`PERSISTENCE.md`](PERSISTENCE.md) | PGN、对局 JSON 与日志格式，以及如何续局 |

第一次阅读建议的顺序：`DESIGN.md` → `TOOLS.md` → `LLM-LOOP.md` →
`PERSISTENCE.md`。

本目录的约定：

- 文件名使用大写，因为这些文档是给人看的，不是给构建系统看的。
- 代码路径（`src/...`）、命令行参数、JSON 键名和日志格式保持原样，方便你直接
  在源码里搜索。
- 描述**行为**的内容归代码所有：如果文档与源码冲突，以源码为准 —— 请修正文档。
- 目录外：安装、运行和命令行参数见 [`README_zh.md`](../README_zh.md)。
