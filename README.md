<img src="assets/icon-128.png" width="96" align="right" alt="招财">

# Dr.Wang Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-Windows-important.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)

[English](README.en.md) · 简体中文

Windows 本地桌面编码 Agent。支持任意 OpenAI 兼容 API，并内置 **Codex CLI** 与 **Claude Code CLI** 可视化面板——一个窗口管三套运行时。

模型与 API 账号解耦：同一模型可挂多个账号 / 网关，可指定账号，也可自动故障切换。密钥进系统凭据库，不写进配置文件。

```
/model ds-reasoner@deepseek-b
/effort high
/context 131072
/mode auto
```

## 功能概览

- 多账号路由与故障切换
- Agent 工作流：`Orchestrate` / `Challenge` / `Research` / `Delegate`
- Plan 模式（写操作可物理拦截，批准后解锁）
- 上下文压缩（可展开、可 `/uncompact`）
- Codex / Claude 原生 CLI 面板（多会话、审批、图片输入）
- 面板内换 API（供应商模板 + `base_url` / key）
- MCP、Skill、定时任务、桌面控制 / 内置浏览器（可选）
- 桌面窗口（WebView2）+ 终端 TUI

## 内置游戏工作室工具包

除编码 Agent 外，本仓库附带我们运营「一名创始人 + 多 AI 员工」游戏工作室的工具：

- `skills/worldclaw-openworld/` —— 文本生成 3D 开放世界的 Agent 蓝图（对腾讯混元 WorldClaw 的方法论复现）：语义布局 → 可复用 Hunyuan3D / Tripo3D 资产 → 地形 → 位姿恢复 → 租约保护的 Blender MCP 装配。
- [`docs/AI_STUDIO_PLAYBOOK.en.md`](docs/AI_STUDIO_PLAYBOOK.en.md) —— 多 AI 游戏生产运营手册：四角色（前台 / 制作 / 异模型验收 / 集成）、一句话信箱派单、分阶段验收、一人一 worktree 的 Git 纪律与门禁纪律。从一次真实的 23 天失败旗舰会话（244 次压缩、约 2.7 万次工具调用、交付 0 张地图）中提炼，重构为能交付的生产线。

## 三个面板

| 面板 | 运行时 | 用途 |
|---|---|---|
| **Agent** | 本项目自研循环 | 多账号、工作流、Plan、压缩、定时任务 |
| **Codex** | 原生 [Codex CLI](https://github.com/openai/codex) | 保留窗口与审批的 Codex |
| **Claude** | 原生 Claude Code CLI | 订阅登录或第三方 API key |

## 安装

**下载版**：解压后运行 `Dr.Wang.exe`（桌面窗口）。手测清单见 [`SMOKE.md`](SMOKE.md)。

| 命令 | 界面 |
|---|---|
| `Dr.Wang.exe` / `aih` / `aih gui` | 桌面窗口 |
| `aih tui` | 终端 |
| `aih gui --serve` | 仅本地服务（浏览器调试） |
| `aih -p "..."` | 无界面单次运行 |

服务只绑定 `127.0.0.1`，启动时生成一次性 token。

**源码版**：

```bash
uv venv && uv pip install -e .
```

## 首次配置

软件不预置模型，也不自动猜测环境。启动后：

```
/accounts add ds https://api.deepseek.com/v1 DEEPSEEK_API_KEY
/models add ds
/role main <模型名>
```

第三个参数是**环境变量名**（不是 key 本身）。然后 `/setup` 检查、`/config save` 保存。

## 打包

```bash
python packaging/build.py --clean
```

产出 `dist/Dr.Wang/` 与 zip。可选安装包（需 [Inno Setup 6](https://jrsoftware.org/isinfo.php)）：

```bash
python packaging/build.py --clean --installer
```

## 许可证与商标

**MIT 许可证**（见 `LICENSE`）：

- 个人 / 研究 / 内部 / 商业使用、修改与分发均可

「Dr.Wang」「Dr.Wang Agent」与招财猫 logo 为作者保留商标。派生作品须改名并更换标识。详见 [`NOTICE`](NOTICE)。

## 安全

报告漏洞见 [`SECURITY.md`](SECURITY.md)。
