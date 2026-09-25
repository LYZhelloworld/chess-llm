# docs

English documentation for chess-llm. Start here and pick a topic.

| Document | What it covers |
| --- | --- |
| [`DESIGN.md`](DESIGN.md) | architecture, layer map, module map, game modes, configuration |
| [`TOOLS.md`](TOOLS.md) | the exact JSON contract of every tool the LLM can call |
| [`LLM-LOOP.md`](LLM-LOOP.md) | how one agent turn runs: prompts, nudges, context window, fallback |
| [`PERSISTENCE.md`](PERSISTENCE.md) | PGN, game JSON and log formats, plus resuming a game |

Reading order for a first pass: `DESIGN.md` → `TOOLS.md` → `LLM-LOOP.md` →
`PERSISTENCE.md`.

Conventions used in this folder:

- File names are upper case because these documents are written for humans, not
  for a build system.
- Code paths (`src/...`), CLI flags, JSON keys and log formats are kept verbatim
  so you can search the source for them.
- Anything that describes *behaviour* is owned by the code: if a document and
  the source disagree, the source is right - please fix the document.

Outside this folder: install, run and CLI flags live in
[`README.md`](../README.md).
