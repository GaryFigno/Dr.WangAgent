<img src="assets/icon-128.png" width="96" align="right" alt="招财">

# Dr.Wang Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-Windows-important.svg)]()
[![License](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-important.svg)](LICENSE)

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

**PolyForm Noncommercial License 1.0.0**（见 `LICENSE`）：

- 允许个人 / 学习 / 研究 / 内部试用等非商业使用、修改与分发
- 不允许商业用途；商业授权请通过仓库 Issue 联系

「Dr.Wang」「Dr.Wang Agent」与招财猫 logo 为商标，不在许可证授权范围内。派生作品须改名并更换标识。详见 [`NOTICE`](NOTICE)。

## 安全

提交前请确认没有 `.env`、密钥、本机 `config.yaml`、收款码等敏感文件被跟踪。报告漏洞见 [`SECURITY.md`](SECURITY.md)。
