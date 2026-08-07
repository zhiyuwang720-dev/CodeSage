<p align="center">
  <img src="assets/logo.png" alt="CodeSage" width="240"/>
</p>

<p align="center"><strong>English</strong> · <a href="README.md">中文</a></p>

# CodeSage

You know that feeling when you ask an AI tool about a complex codebase, and it drowns you in a 500-page essay you'll never read? CodeSage is the opposite. It explains complex projects in plain, human language — like a senior dev sitting next to you. And when it writes code, it writes the minimum amount needed to get the job done. No over-engineering. No fluff. Just clarity and efficiency.

## What is CodeSage?

CodeSage is a Python harness framework in the spirit of Claude Code / Kode-CLI — a structured, staged reimplementation built for two purposes:

1. **Learning**: rebuild every module of a harness step by step to understand how it works end to end
2. **Future adaptation**: a solid foundation for large-project authoring and security-oriented extensions (permissions, auditing, sandboxing)

## Status

| | |
|---|---|
| Phases 01–09 delivered | config, AI clients, tools, core messages, permissions, engine, CLI, context, hooks |
| Tests | 793 passing / 9 skipped (LLM integration, auto-skip without API key) |
| Language | Python ≥ 3.11 + asyncio, httpx, pydantic |

Each phase is one module on its own branch, merged back to `master` only when fully tested.

## Quick Start

```bash
# Run all tests (from repo root or codesage/)
python -m pytest codesage/tests/ -q

# Run a single module
python -m pytest codesage/tests/hooks/ -q

# Check the version
python -m codesage.cli --version

# Include LLM integration tests (skipped without the key)
DEEPSEEK_API_KEY=xxx python -m pytest codesage/tests/ -q
```

## Project Layout

```
codesage/            # production harness (the only active code)
  config/            # settings layering (user/project/local) + global config
  ai/                # LLM clients: adapters, retry, cost, model pointers, VCR
  tools/             # tool contracts + registry + builtin tools (12)
  core/              # messages & sessions
  permissions/       # decision chain: deny > ask > allow, audit
  engine/            # agent main loop, compaction, task queue
  cli/               # interactive REPL
  context/           # AGENTS.md collection, system prompt assembly
  hooks/             # 8 lifecycle events, command/prompt/http executors, `if` rules
Kode-CLI/            # reference implementation (TypeScript, read-only)
docs/                # intent / ideas / specs / modules — spec-driven development
```

## Documentation

- `docs/specs/codesage.md` — master spec: 19-phase roadmap, design invariants
- `docs/specs/0N-*.md` — per-phase specs (read before implementing a phase)
- `docs/modules/` — per-phase comprehension documents
- `tasks/todo.md` — acceptance checklist

## Roadmap

| # | Phase | Status |
|---|---|---|
| 01 | Configuration | ✅ delivered |
| 02 | LLM clients | ✅ delivered |
| 03 | Tools | ✅ delivered |
| 04 | Messages & sessions | ✅ delivered |
| 05 | Permissions | ✅ delivered |
| 06 | Engine main loop | ✅ delivered |
| 07 | CLI REPL | ✅ delivered |
| 08 | Context management | ✅ delivered |
| 09 | Hook system | ✅ delivered |
| 10 | Context compaction | next |
| 11–19 | Tasks, sessions, subagents, skills, MCP, bash safety, memory, multimodel, plugins | planned |

## License

Not yet licensed — ask before reuse.
