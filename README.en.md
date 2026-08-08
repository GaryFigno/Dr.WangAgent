<img src="assets/icon-128.png" width="96" align="right" alt="zhaocai mascot">

# Dr.Wang Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-Windows-important.svg)]()
[![License](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-important.svg)](LICENSE)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4%EF%B8%8F%20Dr.Wang%20Agent-ff69b4.svg)](https://github.com/sponsors/GaryFigno)

English · [简体中文](README.md)

A local desktop coding agent that runs on **any OpenAI-compatible LLM API** and
ships with **visual panels for Codex CLI and Claude Code CLI** — one window,
three agent runtimes.

The key idea: **models and API accounts are decoupled.** The same model can be
backed by several accounts (same URL, different keys, or different gateways).
Pick an account per model, or let the router auto-select and fail over. The
Codex / Claude panels let you switch API providers the same way: choose a
vendor template, paste a `base_url` and key, and the CLI config is generated
for you — keys go to the system credential store, never into config files.

```
/model ds-reasoner@deepseek-b     # model + account
/effort high                      # reasoning effort
/context 131072                   # context window budget
/mode auto                        # permission mode
```

---

## Highlights

- **Multi-account routing** — one model on many accounts / gateways; automatic
  failover, no repeated retries against dead accounts
- **Multi-agent workflows** — `Orchestrate` split → parallel → adversarial
  review → repair → verify; `Challenge` two-stage adjudication; `Research`
  concurrent investigation; `Delegate` cheap-model subtasks
- **Plan mode** — complex requests go through a plan; writes are physically
  blocked by the permission engine until approved
- **Visual context compression** — compressed spans stay expandable,
  `/uncompact` restores full text; usage broken down by source
  (messages / tools / MCP / prompts)
- **Multi-agent teams** — `SpawnAgent` with independent identity / model /
  persisted sessions; `owns` file ownership prevents agents stepping on each other
- **Codex / Claude CLI panels** — native Codex CLI and Claude Code CLI hosted in
  the window: isolated sessions, approval popups, image input, orphan-process
  reaping
- **Switch APIs without touching config files** — vendor templates
  (Kimi, Zhipu GLM, Gemini, Grok, OpenAI, Anthropic…) plus `base_url` + key in
  the UI; automatic Responses ↔ Chat protocol bridging; keys live in the OS
  credential store only
- **Desktop control + built-in browser** — screenshot / click / type and an
  embedded browser (off by default, per-action approval)
- **MCP ecosystem** — plug in existing MCP servers; tool usage per server
- **Skill system** — built-in skill loading; workflow learning distills
  repeated patterns into skills
- **Scheduled jobs + heartbeat self-iteration** — hard caps on rounds / cost /
  time / consecutive failures
- **Dual UI** — desktop window (WebView2) + terminal TUI on one backend
- **Privacy first** — binds `127.0.0.1` only, one-time token per launch; keys
  go to the system credential store, never into config files
- **Desktop pet** — a "zhaocai" cat whose animations follow agent state
  (thinking / working / done / error / tidying memory)

---

## Three workspaces: Agent / Codex / Claude

A switcher at the top of the window toggles between three panels. They share
the same backend and permission model, but sessions, workspaces and processes
are fully isolated — no crosstalk:

| Panel | What runs | Typical use |
|---|---|---|
| **Agent** | This project's own agent loop (`providers/` router + `agent/` loop) | multi-account routing, multi-agent workflows, plan mode, compression, jobs |
| **Codex** | Native [Codex CLI](https://github.com/openai/codex) via its app-server protocol | use Codex with a window, multiple sessions and an approval UI |
| **Claude** | Native Claude Code CLI (`claude` binary) | use Claude Code with subscription login or third-party API keys |

### Codex panel

- **Vendor templates** — Kimi (Moonshot), Zhipu GLM, Gemini, xAI Grok, OpenAI,
  each with a `base_url` and a known-model table; or paste any
  OpenAI-compatible endpoint manually
- **Zero-config API switching** — pick a template / fill in `base_url` + key in
  the UI; the Codex `config.toml` is generated for you. Keys go to the system
  credential store; templates only reference `${ENV}`
- **Automatic protocol bridging** — Codex speaks the Responses API, while
  providers like Kimi only offer the chat interface; a built-in adapter maps
  between them transparently (`aiharness/gui/responses_bridge.py`)
- **One-click import** — import an account already configured in the Agent
  panel into a Codex profile (model, key, endpoint); falls back to a built-in
  model catalog when `/models` is unreachable
- **Isolated sessions** — one process + one `thread_id` per session, concurrent
  by design; state persists, orphan processes are reaped on startup

### Claude panel

- **Two auth modes** — Anthropic subscription login (via `claude auth login`),
  or a third-party API key (Kimi Coding and others speak the Anthropic-compatible
  protocol) — the latter needs nothing but a key pasted in the UI
- **Thin-host design** — it does not reimplement the agent loop; it forwards
  text + images, surfaces approvals / questions in the UI, and isolates
  sessions, so behavior matches the stock CLI
- **Auto-repair** — broken profiles (vendor moved / renamed a model) are fixed
  against known templates automatically, no hand-editing config files

---

## Install

**Download** (no Python needed): unzip `Dr.Wang-x.y.z-windows.zip`, run
`Dr.Wang.exe` — it opens the desktop window. See [`SMOKE.md`](SMOKE.md) for the
manual test checklist.

Both UIs share one backend:

| Command | UI |
|---|---|
| `Dr.Wang.exe` / `aih` / `aih gui` | Desktop window (default) |
| `aih tui` | Terminal UI |
| `aih gui --serve` | Local server only, open in a browser (front-end dev) |
| `aih -p "..."` | Headless, run one prompt and exit |

The window renders with **WebView2** — that is Chromium, already present on
Windows 11, so no bundled browser (~15 MB instead of ~150 MB). The front end is
plain HTML/CSS/JS.

**From source:**

```bash
uv venv && uv pip install -e .
```

## First-run configuration

The software ships with **no preconfigured model and does not probe your
environment** — it will not read environment variables you did not set, and
will not auto-detect local APIs. This is deliberate: a wrong guess bills an
account you never chose.

```bash
/accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY
/models add ds
/role main <model>
```

The third argument is an **environment variable name**, not the key itself —
keys are never written into config files (only `${VAR}` references are). The
account is verified online before it is saved. `/setup` shows what is missing,
`/config save` writes it to disk.

## Package

```bash
python packaging/build.py --clean
```

Produces `dist/Dr.Wang/` (~60 MB) and `Dr.Wang-*-windows.zip` (~39 MB).
PyInstaller onedir mode (not onefile — single-file unpacking is slower and
trips antivirus more often). Optional installer with Inno Setup:

```bash
python packaging/build.py --clean --installer
```

---

## Workflows

| Command | What it does |
|---|---|
| `Delegate` | send a self-contained subtask to a cheaper model |
| `Task` | run a sub-agent with a specific model + account |
| `Research` | multiple models investigate one question **concurrently**, then synthesize and flag disagreements |
| `Challenge` | an adversarial model attacks your proposal; another model re-reads the code and adjudicates each point CONFIRMED / WRONG |
| `Verify` | run the project's checks + a reviewer model judges PASS / FAIL |
| `Orchestrate` | split → parallel execution → adversarial review → auto-repair → verify |

`Challenge`'s second stage matters: adversarial models tend to invent problems,
so another model goes back to the code and only CONFIRMED findings count.

`Orchestrate` lets you assign a different model to each phase:

```
Orchestrate(goal="...", planner_model="ds-reasoner",
            worker_model="ds-chat@deepseek-b", reviewer_model="kimi")
```

## Context & compression

Compression is done **by an LLM the software calls**, not by the model itself.
When the estimated token count exceeds the threshold, a cheap `compactor`
model summarizes old messages into a handoff note.

**Nothing is lost.** Storage has two layers:

- **Record**: `messages.jsonl`, append-only, never rewritten
- **View**: the subset actually sent to the model, shrunk only by compression

Compression leaves a marker saying "messages 0..N are represented by this
summary". `/uncompact` drops the marker and restores the full text; `/history`
always shows the raw record. Only `/clear` and `/delete` actually erase data.

Compression leaves a visible divider in the conversation:

```
━━ context compressed · 40 messages → summary · 47,231 → 12,880 tokens (saved 34,351) ━━
                click to expand · /uncompact restores full text
```

It is the only thing that changes what the agent "knows" without being asked,
so it must leave a trace rather than a log line. `/markers off` disables it.

## Context breakdown

`ctrl+g` or `/context`:

```
Context window  691.0k / 1.0M (69%)
████████████████████████████████████████████

■ Messages          673.4k   67.3%
■ System tools        7.3k    0.7%
■ MCP tools           6.9k    0.7%
■ System prompt       3.5k    0.4%
■ Skills              3.3k    0.3%
■ Free space        309.0k   30.9%

MCP tools by server
   github             3.8k
   playwright         2.1k
```

"69% full" tells you there is a problem, not what it is. **Per-source
breakdown** is what lets you act: forty MCP tools you never call and a
conversation that deserves compression are different problems with different
fixes. MCP usage is broken down per server — surprise bloat usually hides there.

## Heartbeat self-iteration

```
/heartbeat make the whole test suite pass --iterations 20 --cost 3 --minutes 60
```

The agent is pushed toward a goal on a schedule without you typing "continue"
twenty times. It survives disconnects: a failed beat backs off and retries
instead of losing work already on disk.

**This is the most dangerous feature in the software** — it removes the human
from the "spend money + modify files" loop. Hence four hard caps, enforced in
code rather than by prompt: rounds, cost (USD), wall-clock time, consecutive
failures. Whoever hits first stops; no auto-renewal. The model may declare the
goal done or ask for a human, but it must state what it verified — the prompt
is engineered to force this.

## Permissions

Three modes, switchable with `/mode`:

| Mode | Behavior |
|---|---|
| `ask` | confirm writes and commands; allowlists supported |
| `auto` | free read/write inside the workspace; dangerous commands still blocked |
| `yolo` | no prompts; `block_catastrophic` still rejects `rm -rf /`, disk formatting, fork bombs |

Claude-Code-style rules:

```yaml
allow:
  - "Read(*)"
  - "Bash(git diff:*)"      # prefix match
  - "Bash(npm run *)"       # glob
deny:
  - "Bash(sudo:*)"
```

## Scheduled jobs

```
/job add dependency-check | weekly mon,thu 09:30 | check outdated deps, write findings to NOTES.md
/job add daily-report     | daily 18:00          | summarize today's git commits
/job add patrol           | every 4h             | run tests, log failures to FAILURES.md
```

## Command reference

| Command | Purpose |
|---|---|
| `/model [model[@account]]` | view / switch model and account |
| `/models` `/accounts` | list models / accounts with live status |
| `/effort` `/context` | reasoning effort / context budget |
| `/mode` `/allow` | permission mode / allowlist |
| `/new` `/sessions` `/resume` | session management |
| `/clear` `/delete [id\|all]` | clear / delete (with confirmation) |
| `/history` `/compact` `/uncompact` | full record / compress now / restore |
| `/cost` | usage, cache hits, spend by model and role |
| `/job ...` | scheduled jobs |
| `/skills` `/reload` | skills |
| `/mcp [tools\|reconnect]` | MCP servers and tools |
| `/plan [on\|off]` `/approve` | plan mode / approve plan |
| `/classify [on\|off]` | automatic complexity routing |
| `/learn [save <name>]` | distill a skill from session history |
| `/theme` `/pet` `/markers` | colors / desktop pet / compression markers |
| `/doctor` | probe all accounts |

Shortcuts: `ctrl+c` interrupt · `ctrl+d` quit · `ctrl+b` sidebar · `ctrl+r`
show/hide thinking · `ctrl+l` clear screen (conversation kept)

## CLI

```bash
aih                          # interactive
aih -p "fix the login bug"   # one-shot, output to stdout
aih -m ds-chat@deepseek-b     # start with a specific model and account
aih --mode yolo               # override permission mode
aih sessions                  # list sessions
aih jobs / aih run-job <id>   # scheduled jobs
aih daemon                    # scheduler only, no UI
aih doctor                    # health check
```

---

## License & trademark

This repository is licensed under the **PolyForm Noncommercial License 1.0.0**
(see `LICENSE`):

- ✅ Allowed: **noncommercial** use — personal, learning, research, internal
  evaluation — including viewing, modifying, and redistributing the code
- ❌ Not allowed: **commercial use** (including building commercial products or
  services on this code). For a commercial license, contact the author via
  GitHub Sponsors or a repository issue

**"Dr.Wang Agent" and the zhaocai-cat logo are trademarks of the author and are
not covered by this license.** Derivative works must use a different name and
visual identity and must not imply affiliation with the original author.

## Key security (read before committing)

The repository ships with a `.gitignore` that excludes:

- `.env`, `credentials.json`, local `config.yaml` / `.aiharness.yaml`
- runtime state directories `.aiharness/`, `.claude/`
- **API-bearing skills**: put them under `skills.private/` (whole directory
  ignored), or inside a skill use `.env` / `secrets/` / `*api*key*`

Public skill bodies should only say "read the key from an environment
variable" — never embed real keys. See [SECURITY.md](SECURITY.md) for how to
report a vulnerability.

---

## Support the project

Free software, kept alive by voluntary sponsors. A ⭐ and a coffee are both
welcome:

[![Sponsor on GitHub](https://img.shields.io/badge/GitHub_Sponsors-%E2%9D%A4%EF%B8%8F-ff69b4?logo=github)](https://github.com/sponsors/GaryFigno)

Sponsorship is purely voluntary and unlocks nothing.

Inside the app: **Settings → About & support → "Support the author"**.
