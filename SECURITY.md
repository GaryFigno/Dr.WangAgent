# 安全策略 / Security Policy

## 报告漏洞 / Reporting a Vulnerability

本项目暂未启用 GitHub 私有漏洞报告（Private vulnerability reporting）。

发现安全问题时，**不要**创建公开 Issue 或提交包含敏感信息的 PR。请开一个
不含细节的 Issue，标题写明「安全问题，需要私聊」，作者会与你单独联系。

When you find a security issue, **do not** open a public issue or PR that
contains the sensitive details. Open a minimal issue titled
“security issue, contact privately” and the author will reach out.

## 提交前自查 / Pre-commit checklist

本仓库的 `.gitignore` 已排除大多数风险，但提交前请再确认：

- [ ] 没有 `.env`、`credentials.json`、`config.yaml`、`.aiharness.yaml` 被跟踪
- [ ] `skills/` 下没有任何真实 API key（公开 skill 只写「从环境变量读密钥」）
- [ ] `git status --short` 里没有意外出现的密钥/日志文件
- [ ] 推送前可运行密钥扫描（如 `trufflehog3 filesystem .`）

## 攻击面 / Attack surface

本软件能执行 shell 命令、修改文件，本地服务端口虽只绑定 `127.0.0.1` 且每次
启动生成一次性 token，但它仍是高权限程序：

- 桌面控制、内置浏览器、市场模拟盘等能力默认关闭，启用前请阅读对应文档
- 浏览器插件把页面内容视为不可信输入，会拒绝填写密码/银行卡/token 输入框
- 桌宠、MCP、skill 等第三方内容可能包含提示词注入，按不可信数据处理

This software can execute shell commands and modify files. The local server
binds `127.0.0.1` only and issues a one-time token per launch, but it is still
a high-privilege program: desktop control, the built-in browser, and other
optional capabilities are off by default — read their docs before enabling.
Third-party content (MCP servers, skills, web pages) may contain prompt
injection and must be treated as untrusted input.
