<img src="assets/icon-128.png" width="96" align="right" alt="zhaocai mascot">

# Dr.Wang Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-Windows-important.svg)]()
[![License](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-important.svg)](LICENSE)

English · [简体中文](README.md)

A local Windows desktop coding agent for **any OpenAI-compatible LLM API**, with visual panels for **Codex CLI** and **Claude Code CLI** — one window, three runtimes.

Models and API accounts are decoupled: one model can sit on several accounts / gateways, with optional auto-failover. Keys go to the OS credential store, not config files.

```
/model ds-reasoner@deepseek-b
/effort high
/context 131072
/mode auto
```

## Features

- Multi-account routing and failover
- Agent workflows: `Orchestrate` / `Challenge` / `Research` / `Delegate`
- Plan mode (writes can be physically blocked until approved)
- Context compression (expandable, `/uncompact`)
- Native Codex / Claude CLI panels (sessions, approvals, images)
- In-panel API switching (vendor templates + `base_url` / key)
- MCP, Skills, scheduled jobs, optional desktop control / browser
- Desktop window (WebView2) + terminal TUI

## Three panels

| Panel | Runtime | Use |
|---|---|---|
| **Agent** | This project's loop | routing, workflows, plan, compression, jobs |
| **Codex** | Native [Codex CLI](https://github.com/openai/codex) | Codex with a window and approvals |
| **Claude** | Native Claude Code CLI | subscription login or third-party API keys |

## Install

**Binary**: unzip and run `Dr.Wang.exe`. Smoke checklist: [`SMOKE.md`](SMOKE.md).

| Command | UI |
|---|---|
| `Dr.Wang.exe` / `aih` / `aih gui` | Desktop window |
| `aih tui` | Terminal |
| `aih gui --serve` | Local server only (browser debug) |
| `aih -p "..."` | Headless one-shot |

The server binds `127.0.0.1` only and issues a one-time token per launch.

**From source**:

```bash
uv venv && uv pip install -e .
```

## First-time setup

No models are preconfigured, and the app will not guess your environment:

```
/accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY
/models add ds
/role main <model-id>
```

The third argument is an **environment variable name**, not the raw key. Then `/setup` and `/config save`.

## Build

```bash
python packaging/build.py --clean
```

Optional installer (requires [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```bash
python packaging/build.py --clean --installer
```

## License & trademarks

**PolyForm Noncommercial License 1.0.0** (`LICENSE`):

- Allowed: personal / learning / research / internal noncommercial use, modification, and distribution
- Not allowed: commercial use; contact via GitHub Issues for commercial licensing

“Dr.Wang”, “Dr.Wang Agent”, and the zhaocai cat logo are trademarks and are **not** licensed under PolyForm. Derivatives must use a different name and visual identity. See [`NOTICE`](NOTICE).

## Security

To report a vulnerability, see [`SECURITY.md`](SECURITY.md).
