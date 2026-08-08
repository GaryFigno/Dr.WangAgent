# TODO：Agent 内核参考调研

目标：不整核替换，把成熟 harness 当参考书，把策略落到 `aiharness`（Python）。

---

## 落地状态

### 已完成的优化（OpenCode / Aider / Claude Code 对照）

| 项 | 来源 | 落点 |
|----|------|------|
| Tool-output prune + 结构化 digest | OpenCode | `agent/context.py` |
| Bash / Read 分级摘要进 wire | OpenCode | `prepare_tool_result_for_model` + `loop._record` |
| Microcompact 重复 Read | OpenCode | `microcompact_reads` |
| Read 未改则缓存 stub | OpenCode | `tools/fs.py` Read `force` |
| Doom-loop | OpenCode | `agent/loop.py` `_invoke` |
| Stream 只读预取 | OpenCode | `loop.py` `_maybe_start_prefetch` |
| allow / ask / persistent always | OpenCode / CC | `permissions.py` + GUI/TUI |
| Overflow → compact → retry | OpenCode | `loop.py` |
| 压缩后 tool re-announce | OpenCode | `_post_compact_reminder` |
| Snapshot on rewind | OpenCode | `commands._rewind_turn` |
| Bash 路径级权限 | OpenCode / CC | `extract_command_paths` |
| Session reminders | OpenCode | `_ephemeral_reminder` |
| Compaction tail token budget | OpenCode | `preserve_recent_tokens` |
| Explore 只读模式 | OpenCode / CC | `explore_mode` |
| Retry jitter / empty stream | OpenCode | `providers/router.py` |
| `.gitignore` / `.aiharnessignore` | Aider / CC / rg | `workspace/ignore.py` → Glob/@/tree/search |
| `git ls-files --exclude-standard` 索引 | Aider | `workspace/paths.py` |
| Repo map（查询+脏文件排名） | Aider | `workspace/repomap.py` → env note |
| 更富的 git note（脏路径+近期 commit） | Aider / CC | `prompts._git_summary` |
| 嵌套 `AGENTS.md` / `CLAUDE.md` | Claude Code | `prompts.read_project_instructions` |

### 明确不做 / 暂缓

- 整仓替换 OpenCode / Claude Code  
- Effect/TS 双栈内核  
- LSP 包、Plugin 生态（需求不强时）  
- 复制 Anthropic 专有源码  
- Fuzzy / indent-tolerant Edit（下一刀）  
- Skill `allowed-tools` 硬执行（下一刀）  
- 回合后自动抽 memory（下一刀）  
- 超大工具结果再调 cheap 模型二次摘要（启发式 digest 已覆盖主路径）

---

## 使用提示

- Explore：`/explore on`（TUI）或 GUI 进入后点 **EXPLORE** 徽章退出  
- Plan：`/plan`（与 Explore 互斥）  
- 回退用户轮次会同时尝试还原待审磁盘改动  
- Read 同一文件未改动时返回 `[unchanged]`；需要全文时传 `force=true`  
- 工作区根放 `.gitignore` / `.aiharnessignore`；`@` 索引优先走 git exclude-standard  

## 测试

- `tests/test_bash_paths.py`  
- `tests/test_compact_tail.py`  
- `tests/test_context_prune.py` / `test_tool_digest.py` / `test_doom_loop.py`  
- `tests/test_ignore_and_repomap.py`  
- `tests/test_gui.py::test_rewind_turn_restores_pending_disk_edits`  
