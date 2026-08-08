---
name: git-commit
description: 写规范的 git 提交信息。当用户要求提交代码、写 commit message、或提交前整理改动时使用。产出 Conventional Commits 格式的中文提交信息。
allowed-tools: [Bash, Read, Grep]
---

# 规范 git 提交

## 步骤

1. 先看改动范围，绝不盲写：
   ```bash
   git status --short
   git diff --stat
   ```
2. 检查是否混入了不该提交的东西（密钥、构建产物、日志）。发现 `config.yaml`、
   `.env`、`credentials.json`、`dist/`、`build/` 等，先让用户确认，不要自行加入。
3. 按改动写提交信息，格式：

   ```
   <type>(<scope>): <中文简述>
   ```

   - `feat` 新功能
   - `fix` 修 bug
   - `refactor` 重构（不改行为）
   - `docs` 文档
   - `test` 测试
   - `chore` 杂务（依赖、构建、格式化）

   scope 是可选的影响模块，如 `feat(quest): 步骤失败自动重试`。

4. 一次提交只做一件事：改动跨多个无关主题时，用 `git add <path>` 分开提交。
5. 提交前展示命令让用户确认（除非已获授权直接执行）：
   ```bash
   git commit -m "<message>"
   ```

## 红线

- 提交前必须 `git status` 确认暂存区范围
- 不提交 `.git`、`node_modules`、`.venv`、`dist/`、`build/`、密钥与日志文件
- 不替用户改写他人未提交的改动
