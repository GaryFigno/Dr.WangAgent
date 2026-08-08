# Git 工作规则

1. 提交前先 `git status --short` 和 `git diff --stat` 确认改动范围。
2. 提交信息用 Conventional Commits 格式：`<type>(<scope>): <中文简述>`。
3. 不提交密钥、构建产物（`dist/ build/`）、依赖目录（`.venv/ node_modules/`）和日志。
4. 一次提交只做一件事；无关改动分开提交。
5. 不替用户覆盖别人未提交的改动；合并/推送前先说明影响。
