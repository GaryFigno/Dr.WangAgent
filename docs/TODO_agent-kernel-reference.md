# TODO：Agent 内核参考调研

目标：不整核替换，把成熟 harness 当参考书，把策略落到 `aiharness`（Python）。

---

## 落地状态

### 已完成的优化（OpenCode 对照）

| 项 | 落点 |
|----|------|
| Tool-output prune | `agent/context.py` `prune_old_tool_outputs` |
| Doom-loop | `agent/loop.py` `_invoke` |
| Stream 只读预取 | `loop.py` `_maybe_start_prefetch` |
| allow / ask / persistent always | `permissions.py` + GUI/TUI |
| Overflow → compact → retry | `loop.py` |
| **Snapshot on rewind** | `commands._rewind_turn` → `edit_review.reject_all` |
| **Bash 路径级权限** | `extract_command_paths` + `check()` |
| **Session reminders** | `_ephemeral_reminder` every N turns |
| **Compaction tail token budget** | `preserve_recent_tokens` in `_split_for_compaction` |
| **Explore 只读模式** | `explore_mode` + `/explore` + GUI badge |
| **Retry jitter / empty stream** | `providers/router.py` |

### 明确不做 / 暂缓

- 整仓替换 OpenCode / Claude Code  
- Effect/TS 双栈内核  
- LSP 包、Plugin 生态（需求不强时）  
- 复制 Anthropic 专有源码  

---

## 使用提示

- Explore：`/explore on`（TUI）或 GUI 进入后点 **EXPLORE** 徽章退出  
- Plan：`/plan`（与 Explore 互斥）  
- 回退用户轮次会同时尝试还原待审磁盘改动  

## 测试

- `tests/test_bash_paths.py`  
- `tests/test_compact_tail.py`  
- `tests/test_context_prune.py` / `test_doom_loop.py`  
- `tests/test_gui.py::test_rewind_turn_restores_pending_disk_edits`  
