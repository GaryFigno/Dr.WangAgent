# 贡献指南 / Contributing

欢迎贡献！在动手前请先读这一页——本项目是**非商业开源**，规则不多但都重要。

Welcome! Before you start, please read this page. This project is
**noncommercial open source** — the rules are few but important.

## 许可证说明 / License notice

本仓库采用 **PolyForm Noncommercial License 1.0.0**（见 `LICENSE`）。

**你提交的代码默认按仓库许可证授权。** 这意味着你的贡献只能用于非商业用途；
如需商业授权，需要作者另行安排。不接受这一点请不要提交。

By submitting a PR you agree that your contribution is licensed under the
repository license (PolyForm Noncommercial 1.0.0). Contributions are therefore
noncommercial-only; a commercial license requires separate arrangements with
the author. Please do not submit if you do not agree.

## 开发环境 / Dev setup

```bash
uv venv && uv pip install -e ".[dev,desktop]"
pytest            # 跑测试
ruff check .      # lint（配置在 pyproject.toml）
```

要求：Python 3.10+，Windows（本项目目前以 Windows 为目标平台）。

## 提交规范 / Commit conventions

- 提交信息用 Conventional Commits：`<type>(<scope>): <中文或英文简述>`
- 一次提交只做一件事；无关改动分开提交
- 提交前 `git status --short` + `git diff --stat` 确认改动范围
- **绝不提交**：密钥、`.env`、`credentials.json`、`config.yaml`、
  收款码图片（`alipay.png` / `wechat.png`）、`dist/`、`build/`、`.venv/`

## 代码风格 / Style

- 行宽 100；遵循 `pyproject.toml` 里 `[tool.ruff]` 的规则
- 模块 docstring 说明"为什么"而不是"是什么"
- 阈值（轮数上限、冷却时间等）放进 `aiharness/constants.py`，不要散落魔法数字
- 新增功能要有测试（`tests/` 下，参考现有测试的写法）

## 什么不要做 / What not to do

- 不要把任何**含真实 API key** 的 skill、配置或脚本提交进仓库
- 不要提交 `.cursor-tmp/` 之类的本地调试产物（已忽略，不要 `git add -f`）
- 不要为了通过 lint 而禁用有意义的安全检查
