# Dr.Wang Agent — 开发 TODO

来源：Claude Code 会话「Claude 类软件开发」  
会话链：`fc1f725a` → `7073221f` → `98e1a01d` → `e277a668`（最新，于 2026-08-05 因 API Stream idle timeout 中断）  
对照代码快照：当前工作区 `C:\ClaudeProjects\AIHarnessAgent`

---

## 进度总览

**后端核心大体完成**：多账号路由、工作流（Orchestrate / Challenge / Research / Delegate / Verify）、会话 record/view、压缩可见、MCP、Skill、权限三模式、Plan/复杂度分流、心跳、定时任务、浏览器/桌面工具、凭据库、按账号代理、托盘、打包为 `Dr.Wang`。

**GUI 主路径 TODO 已清空**（T00–T30 首版/完成）；可选深化见文末。

| 阶段 | 状态 |
|---|---|
| 项目骨架 / 配置 / Provider 路由 | ✅ 完成 |
| Agent 循环 / 压缩 / 缓存稳定 | ✅ 完成 |
| 工作流 / 多 agent / MCP / Skill | ✅ 完成 |
| TUI（`aih tui`） | ✅ 完成（用户主路径已转向 GUI） |
| GUI 桌面壳（PyWebView + WebView2） | ✅ 基本可用 |
| 打包 `dist/Dr.Wang/` | ✅ 有产物 |
| 最新一轮 GUI/体验打磨 | ✅ 完成 |
| K 线 GUI / Canvas / 后台会话存活 | ✅ MVP 已做 |
| 对话模型跟选型 / Cursor 式布局 | ✅ T32–T33 首版 |
| 去花费显示 / 工具超时 / 权限记忆 | ✅ T34–T37 |
| 本地 Agent/Quest 深化（审阅/检索/@/稳态） | ✅ T38–T45 + 深化一轮 |

---

## 已完成（不必重做，除非回归）

- [x] 多账号 / `model@account` 路由与故障转移
- [x] Orchestrate / Challenge / Research / Delegate / Verify
- [x] 会话永不丢（append-only + 可见压缩）
- [x] Skill（含 GBK）+ MCP 客户端
- [x] Plan 模式权限引擎 + 复杂度分流（后端）
- [x] 心跳自动迭代（四道硬上限）
- [x] 定时任务 + daemon
- [x] 浏览器 / 桌面工具（默认关）+ **设置里授权开关**
- [x] 凭据：粘贴 key → `credentials.json`，不写进 `config.yaml`
- [x] 软件显示名 **Dr.Wang**（内部 slug 仍为 `aiharness`）
- [x] 按账号设置代理（跟随系统 / 直连 / 指定 Clash 等）
- [x] 关闭窗口 → 托盘；托盘「退出程序」才退出（含打包 hiddenimport）
- [x] 心跳 UI：只设硬上限 + 开启；目标走输入框 + 目标模式横幅
- [x] 可删最后一条会话、可零会话；可移除项目目录并持久化
- [x] 工作目录改到新会话时 chip 选择（不再在侧栏全局切换）
- [x] 长工具输出可折叠（`tool-group`）
- [x] 问询多选（`multi_select` / `option-picked`）
- [x] Plan 反馈输入框（`plan-feedback`）
- [x] 同模型多账号合并（不再自动造 `k3-2`）
- [x] Plan 模式放行只读浏览器工具（代码已改；中断前测试未完全收尾）
- [x] Shell 发现：优先真 Bash，避开 WindowsApps WSL 转发器（中断前刚修完语法/测试）

---

## 未完成 TODO（按建议执行顺序）

### P0 — Bug / 正确性（优先）

#### T00. 打断后 `tool_calls` 缺回应 → HTTP 400 卡死 ✅
- **现象（图1）**：对话停住，手动打断后继续提问，反复报  
  `An assistant message with 'tool_calls' must be followed by tool messages…`  
  （DeepSeek / Kimi 均复现，缺 `Grep:67` 等 `tool_call_id`）。
- **根因**：打断时 GUI 会 `task.cancel()`；assistant 已写入 `tool_calls`，但对应 `role=tool` 结果未写入；下一轮请求被供应商拒绝，看起来像「卡住」。
- **已修**（2026-08-05）：
  - `seal_unanswered_tool_calls()`：打断/取消/`run` 收尾时补齐 filler
  - `pair_tool_calls()`：wire 视图自愈（含用户已接着提问的旧会话）
  - 测试：`test_pair_tool_calls_repairs_interrupt_holes`、`test_interrupt_after_tool_calls_seals_results`、`test_run_heals_orphaned_tool_calls_before_calling_model`
- **验收**：打断正在跑工具的一轮 → 立刻再提问 → 不再 400；旧的已损坏会话也能继续聊。  
  （2026-08-06 已重新打包进 `dist/Dr.Wang`。）

#### T01. 收尾并验证 Shell + Plan 模式修复 ✅
- **背景**：会话在修 `shell`（避开 WSL）和 `PLAN_MODE_TOOLS` 时 API 超时。
- **已验**（2026-08-05）：`test_shell_discovery` / `test_permissions` / `test_planning` 71 绿；全量 **560 passed**；ruff 全绿。代码含 WSL shim 排除与 Plan 模式只读浏览器工具。

#### T02. 添加账号/模型后自动落盘 ✅
- **背景**：用户反馈「加了第二个 Kimi 账号，退出重进全消失」。
- **根因**：`_add_account` / `_add_model` 等只 `push_config`，不写盘。
- **已修**（2026-08-05）：`_persist_config()`；账号/模型/角色/代理/能力/skill 路径/自动压缩变更后自动保存；首次配置缺 `main` 角色不挡写入。
- **验收**：添加账号 → 杀进程 → 重启 → 仍在；密钥不进 `config.yaml`。测试：`test_adding_an_account_writes_the_config_without_a_second_click`。

#### T03. 发送后即时状态反馈 ✅
- **背景**：「发出去的瞬间没有任何反馈，以为发送失败」。
- **已修**（2026-08-05）：发送瞬间显示「发送中…」；`turn_start` 起显示 `model@account 思考中…`；工具/回答时更新；结束或 `busy=false` 清除。带脉冲点样式。

#### T04. Plan 模式只在分类为项目时进入 ✅
- **背景**：用户问「为什么一上来就是 plan mode」。
- **根因**：`plan_mode` 挂在整个 GuiSession 上，进过一次后新会话/换会话也不清，PLAN 徽章「粘住」。
- **已修**（2026-08-05）：
  - 新会话 / 打开会话时 `_reset_plan_mode`
  - 点顶栏 PLAN 可退出；设置里「自动识别大项目并进入 Plan 模式」开关
  - 分类角色：`classifier_role` → `cheap` → `fast`，**不用 main 兜底**（大模型爱把小事打成项目）
  - 进入时中文提示
- **验收测试**：`test_new_session_clears_sticky_plan_mode` 等。

---

### P1 — UI 布局与智能化

#### T23. 对话框 / 整体布局对齐 Cursor（图2） ✅（首版）
- **已修**：主栏气氛背景、侧栏收窄、面板宽度、资源/活动/摘要层次；输入区加大（T06）+ 上下文可按来源拆开（T24）。

#### T24. 提升 agent「更智能」——补齐图2 小红框那一层 ✅（首版）
- **已修**：上下文面板拆 system/tools/rules/MCP/skills/messages；全局+项目 rules 注入；AGENTS.md 命中可见；谁在干什么（T09/T27）；工具 lite 裁剪（T30）。

#### T25. Cursor 式「可展开/默认收起」的操作摘要 ✅
- **已修**：智能摘要文案、Edit +/- 与绿/红 diff、默认收起 tool-group；T15 红框视觉并入此项。

### P1 — 最新一批 GUI 体验（用户 12 条 + 补充）

#### T05. 发送键改为 Ctrl+Enter ✅
- **已修**：Ctrl/Cmd+Enter 发送；Enter 换行；placeholder 已更新。

#### T06. 输入框加大 ✅
- **已修**：默认约 96px，自动长高上限 360px，可纵向拖高。

#### T07. 去掉手动「压缩 / 恢复全文」入口 ✅
- **已修**：上下文面板去掉两按钮；说明改为强调自动压缩 + 对话内分隔条。后端 `compact`/`uncompact` 命令仍保留（未从协议删除）。

#### T08. 模型选择器只展示 `model@account` ✅
- **已修**：顶栏与角色指派只列 `model@account`；无绑定时提示去设置绑定。

#### T09. 对话内展示「谁在干什么」 ✅
- **已修**：主模型活动行（T03）+ 子 agent progress → activity；并行多条见 **T27**（已完成）。

#### T10. 输入草稿跨会话 / 重启保留 ✅
- **已修**（2026-08-05）：`DraftStore`（`composer_drafts.json`）按 `session_id` 落盘；`SET_DRAFT` + status 回填；发送/删会话清草稿；前端防抖保存、切换会话/beforeunload flush。
- **验收**：`tests/test_drafts.py` + GUI 草稿用例绿。

#### T11. 粘贴图片进对话（不限数量，保留顺序） ✅
- **已修**（2026-08-05）：
  - Composer Ctrl+V / 拖拽多图，缩略图条按序标注 图1…图N
  - 图片存 `session/attachments/`，`meta.attachments` 保序；重开经 `/attachment/...` 回放
  - 有 vision（`supports_vision` / `vision` tag / 模型名启发）时发 multimodal `image_url`；否则中文降级提示 + 文字占位，原图仍可看
- **验收**：`tests/test_attachments.py`；设置里可给模型开 `supports_vision: true`。

#### T12. 回答中的文件/图片可索引跳转 ✅
- **已修**（2026-08-05）： tool_end 转发 display；资源芯片+OPEN_PATH+/workspace-file
- **验收**: test_tool_end_forwards_display_path, test_open_path_rejects_outside_workspace

#### T13. 回答内容一键复制 ✅
- **已修**（2026-08-05）：代码块「复制」、行内 code 点击复制、工具输出复制。

#### T14. Cache 统计语义澄清或持久化 ✅
- **已修**（2026-08-05）：运行/会话双命中率；会话累计持久化。

#### T15. 图1「红框效果」对齐（视觉） ✅
- **已合并入 T25**：可折叠操作摘要 + Edit +/- diff。

---

### P2

#### T16. 后台会话在关窗后继续跑 ✅
- **已修**：托盘提示明确进行中对话/心跳/子任务继续；侧栏显示「运行中」。

#### T17. Cursor 风格 Canvas ✅（MVP）
- **已修**：画布面板预览 md/html/图片；canvas_hint 自动填路径（T28）。

#### T18. K 线 / 行情 GUI ✅（MVP）
- **已修**：报价 / 近 30 日表 / 纸上账户；不做真实下单。

#### T19. 工作流学习 GUI 入口 ✅
- **已修**：Skill 页「扫描候选 Skill」。

---

### P3

#### T20. 打包回归清单 ✅
- **已修**（2026-08-06）：关闭 `Dr.Wang`/`aih` 进程后 `python packaging/build.py --clean`；产物 `dist/Dr.Wang/`（约 59.8 MB）与 `dist/Dr.Wang-0.1.0-windows.zip`。

#### T21. 测试与风格门禁 ✅
#### T22. 安全提醒 ✅
#### T26. 未配置 cheap/fast 时提示 ✅
#### T27. 并行多条活动条 ✅
#### T28. Canvas 产物自动挂载 ✅（首版）
#### T29. 行情深度 GUI ✅（首版）
#### T30. 工具集动态裁剪 ✅

---

### P4 — 角色语义与 Cursor 式布局（2026-08-06 新开）

#### T31. 角色够用，不新增 shell / 拆分等专用角色 ✅（结论）
- **结论**：现有内置角色已覆盖主路径分工，**不必**再拆「专门跑 shell」「专门拆任务」这类角色。
  - shell / 读写是 **工具**，不是模型岗位；可靠性靠权限模式、Plan、沙箱，不靠换模型。
  - 拆任务 / 并行调研已有 Orchestrate / Research / Delegate + `classifier`→`cheap`/`fast`。
  - 保留：`fast` / `cheap` / `compactor` / `titler` / `verifier` / `adversary` / `researcher`。
- **不做**：新增 `shell` / `planner` / `splitter` 角色（除非以后有明确计费或延迟证据再议）。

#### T32. 对话模型 = 对话框选型；弱化配置里的 `main` 角色 ✅
- **已修**：`Selection.for_session` — 会话 `meta.model` 优先，否则 `roles.main`；打开会话恢复自身选型；新会话回落默认；改设置里的默认不再劫持当前对话；文案改为「默认对话模型」。

#### T33. 图2 布局对齐 Cursor（图3） ✅（首版）
- **已修**：模型/effort/context/mode 下沉到输入区工具条；顶栏只留 PLAN/用量/cache；侧栏收窄 + 设置/上下文图标化；用户/助手气泡块。

---

### P5 — 体验与稳健性（2026-08-06）

#### T34. 顶栏不再显示花费 `$` ✅
- **已修**：去掉顶栏 `#cost-stat`（会话列表仍可显示花费；后端继续记账）。

#### T35. 工具调用超时保护 ✅
- **结论**：需要。Shell/Grep 原有超时；现为所有工具加统一天花板（并行工具 90s，一般 180s，Bash 外层略高于 shell 上限）。

#### T36. 每会话记住 ask / auto / yolo ✅
- **已修**：`meta.permission_mode`；`set_mode` 写入会话并落盘为新会话默认；打开会话恢复该会话模式。

#### T37. 并行工具一卡拖死 turn ✅
- **风险**：成立。已用 T35 的 per-call `wait_for`：并行里一个超时只返回 error，其余照常完成。

---

### P6 — 本地 Agent / Quest 能力（不要云端、不要完整 IDE）

**产品边界**：只要本地 Agent + Quest；不要云端 Agent / 多用户协作 / 完整 IDE。  
**主目标**：稳定智能（可靠完成任务）＞ 编辑器外观。

#### 优先（直接抬「稳定智能」）

#### T38. 编辑审阅流：Apply / Reject（非 IDE） ✅（首版）
- **已修**：Write/Edit 写盘后入队；GUI「待审改动」条支持接受/回滚/全部；同路径 LIFO 回滚；外部改动则拒回滚。
- **非目标**：不做多 tab 编辑器；Bash/MCP 侧写暂不入队。

#### T39. 本地检索 + `@` 引用 ✅（首版）
- **已修**：`@` 补全路径索引；芯片挂载；发送时注入文件/目录摘要；不做向量语义。

#### T40. Rules / Memories UX 做实 ✅（首版）
- **已修**：设置页编辑全局/项目 rules；Memories 钉/删；注入 system 并在上下文面板标出来源。

#### T41. 流式手感 + 中断恢复加固 ✅（首版）
- **已修**：流式 rAF 合并绘制；打开会话提示未完成工具调用；工具超时 toast。

#### T42. Quest / 任务稳态（智能主路径） ✅（MVP）
- **已修**：目标+步骤、阻塞/续跑、条带提示与 prompt hint；存 `.aiharness/quest.json`。

#### 次优先（Agent 壳增强，仍非 IDE）

#### T43. 轻量文件树 + 单文件预览 ✅（首版）
- **已修**：面板「文件」树 + 预览 + 外开；与 `@` 共用路径。

#### T44. 边改边看（Diff 预览面板） ✅（首版）
- **已修**：待审条点「大图」/行 → 文件页大 diff（复用 T38）。

#### T45. 本机多窗口（可选） ✅（首版）
- **已修**：侧栏 ⧉ 再起本机 `aih gui` 进程；独立会话，不云端同步。

#### 明确不做

- 云端 Agent / Background cloud / 账号云同步
- 完整 IDE（多 tab 编辑引擎、LSP、调试器、扩展市场）
- 点选「跳转到定义」若无 LSP——改为「在资源管理器/外部编辑器打开」

### 深化（本轮已做）

- 审阅：统一 diff、Write 预览截断、yolo「自动接受」开关（`ui.auto_apply_edits`）
- `@` 索引：浅 stamp 缓存 + mtime 排序
- Quest ↔ TodoWrite / Verify；失败标 blocked
- 对话气泡「钉为记忆」
- 流式工具块延后折叠；上下文面板「本轮 @」；新窗口可选目录
- Canvas 源码可编辑保存；行情 SVG K 线
- 打包冒烟：`tests/test_opt_pass.py`（托盘 import / 审阅回滚 / Plan 不粘 / 落盘）
- 多语言：设置页可选 Auto / 中英日韩欧亚等；`i18n.js` + `UIPrefs.language`

### 缺口补齐（本轮）

- Quest：断点续跑自动开下一轮；心跳续聊注入 Quest hint；Verify 失败 notice
- Bash 侧写进审阅队列（快照对比）
- 文件内容搜索 + 路径索引 ext/kind 过滤
- 后端 notice / 能力说明随界面语言；首次配置 onboarding 条
- README / 产物名对齐 Dr.Wang；`SMOKE.md` 手测清单
- 行情：MA 回测权益曲线 + 本地价提醒

## 建议下一棒

按 `SMOKE.md` 对 `dist/Dr.Wang` 做一轮手测。
