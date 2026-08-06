"""Server-side GUI notice strings, keyed by UI language preference."""

from __future__ import annotations

from typing import Any

from .locale import normalize_language

#: Compact catalogs — missing keys fall back en → zh → key.
_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "busy": "仍在运行 — 请先打断",
        "interrupted": "已打断",
        "model.busy": "对话进行中，结束后才能切换模型",
        "steer.queued": "已接受引导，将在当前回合的模型间隙注入：{preview}",
        "rewind.ok": "已回退，删除了 {n} 条记录",
        "rewind.restored": "已还原 {n} 处磁盘改动",
        "rewind.missing": "找不到要回退的那一轮",
        "rewind.bad_index": "回退参数无效",
        "rewind.busy": "仍在运行 — 请先打断再回退",
        "interrupting": "正在打断…",
        "quest.resumed": "已从断点续跑 Quest，并开始下一轮",
        "quest.resumed_idle": "已从断点续跑 Quest（当前空闲，可直接提问）",
        "quest.none": "没有可续的 Quest",
        "quest.started": "Quest 已开始：{goal}",
        "quest.blocked": "Quest 已阻塞：{reason}。可点「从断点续」自动重试。",
        "quest.cleared": "已清除 Quest",
        "quest.no_step": "没有这个 Quest 步骤",
        "memory.empty": "记忆内容不能为空",
        "memory.missing": "没有这条记忆",
        "rule.missing": "没有这条规则",
        "rule.saved": "已保存规则 {name}",
        "rule.deleted": "已删除规则",
        "edit.unknown": "未知的审阅操作 '{action}'",
        "canvas.empty_path": "画布路径不能为空",
        "canvas.empty_body": "缺少要保存的内容",
        "canvas.outside": "路径超出工作区",
        "canvas.saved": "已保存画布 {path}",
        "canvas.save_fail": "保存失败：{error}",
        "window.cancel": "已取消打开新窗口",
        "window.bad_dir": "不是目录：{path}",
        "window.fail": "无法打开新窗口：{error}",
        "window.opened": "已打开本机新窗口 · {name}（独立进程与会话）",
        "language.set": "界面语言：{label}",
        "yolo.auto_apply": "yolo · 已自动接受改动（可在设置关闭）",
        "yolo.auto_apply_pending": "已自动接受当前待审改动",
        "bash.review": "Bash 改动了 {n} 个文件，已加入待审",
        "plan.enter": "已进入 Plan 模式",
        "plan.exit": "已退出 Plan 模式，可以正常改文件了",
        "explore.enter": "已进入 Explore 模式（只读调查）",
        "explore.exit": "已退出 Explore 模式",
        "onboard.need_account": "添加 API 账号",
        "onboard.need_model": "为账号添加模型",
        "onboard.need_role": "指定默认对话模型（main 角色）",
        "onboard.ready": "配置完成，可以开始对话",
        "search.empty": "请输入搜索词",
        "alert.added": "已添加提醒：{symbol} {op} {price}",
        "alert.fired": "提醒触发：{symbol} 现价 {price}（条件 {op} {target}）",
        "alert.none": "暂无提醒",
        "backtest.need_symbol": "请输入股票代码",
        "market.disabled": "行情未启用。请在配置里打开 market.enabled。",
    },
    "en": {
        "busy": "Still working — interrupt first",
        "interrupted": "Interrupted",
        "model.busy": "Wait for the turn to finish before switching models",
        "steer.queued": "Guidance accepted — will inject at the next model gap: {preview}",
        "rewind.ok": "Rewound; removed {n} messages",
        "rewind.restored": "Restored {n} disk change(s)",
        "rewind.missing": "That turn was not found",
        "rewind.bad_index": "Invalid rewind index",
        "rewind.busy": "Still working — interrupt before rewinding",
        "interrupting": "Interrupting…",
        "quest.resumed": "Quest resumed from checkpoint; starting next turn",
        "quest.resumed_idle": "Quest resumed (idle — send a prompt when ready)",
        "quest.none": "No Quest to resume",
        "quest.started": "Quest started: {goal}",
        "quest.blocked": "Quest blocked: {reason}. Use Resume to retry.",
        "quest.cleared": "Quest cleared",
        "quest.no_step": "Unknown Quest step",
        "memory.empty": "Memory text cannot be empty",
        "memory.missing": "Memory not found",
        "rule.missing": "Rule not found",
        "rule.saved": "Saved rule {name}",
        "rule.deleted": "Rule deleted",
        "edit.unknown": "Unknown edit action '{action}'",
        "canvas.empty_path": "Canvas path required",
        "canvas.empty_body": "Missing content to save",
        "canvas.outside": "Path outside workspace",
        "canvas.saved": "Saved canvas {path}",
        "canvas.save_fail": "Save failed: {error}",
        "window.cancel": "New window cancelled",
        "window.bad_dir": "Not a directory: {path}",
        "window.fail": "Cannot open window: {error}",
        "window.opened": "Opened local window · {name}",
        "language.set": "Language: {label}",
        "yolo.auto_apply": "yolo · edits auto-accepted (disable in Settings)",
        "yolo.auto_apply_pending": "Accepted all pending edits",
        "bash.review": "Bash changed {n} file(s); queued for review",
        "plan.enter": "Entered Plan mode",
        "plan.exit": "Left Plan mode — writes unlocked",
        "explore.enter": "Entered Explore mode (read-only)",
        "explore.exit": "Left Explore mode",
        "onboard.need_account": "Add an API account",
        "onboard.need_model": "Add a model for an account",
        "onboard.need_role": "Assign the default chat model (main role)",
        "onboard.ready": "Setup complete — you can chat",
        "search.empty": "Enter a search query",
        "alert.added": "Alert added: {symbol} {op} {price}",
        "alert.fired": "Alert: {symbol} at {price} ({op} {target})",
        "alert.none": "No alerts",
        "backtest.need_symbol": "Enter a symbol",
        "market.disabled": "Market disabled. Set market.enabled in config.",
    },
    "ja": {
        "busy": "実行中です — 先に中断してください",
        "interrupted": "中断しました",
        "model.busy": "応答が終わるまでモデルを切り替えられません",
        "rewind.ok": "巻き戻し：{n} 件削除",
        "rewind.missing": "そのターンが見つかりません",
        "rewind.bad_index": "巻き戻し指定が無効です",
        "interrupting": "中断しています…",
        "quest.resumed": "Quest を再開し、次のターンを開始します",
        "quest.resumed_idle": "Quest を再開しました（待機中）",
        "quest.none": "再開できる Quest がありません",
        "quest.started": "Quest 開始：{goal}",
        "quest.blocked": "Quest ブロック：{reason}。「再開」で再試行できます。",
        "quest.cleared": "Quest をクリアしました",
        "language.set": "表示言語：{label}",
        "bash.review": "Bash が {n} ファイルを変更。レビュー待ちに追加",
        "plan.enter": "Plan モードに入りました",
        "plan.exit": "Plan モードを終了 — 書き込み可能",
        "onboard.need_account": "API アカウントを追加",
        "onboard.need_model": "モデルを追加",
        "onboard.need_role": "既定チャットモデル（main）を指定",
        "onboard.ready": "設定完了 — 会話を始められます",
        "market.disabled": "相場が無効です。market.enabled を設定してください。",
    },
}


def resolve_ui_locale(pref: str) -> str:
    """Map preference to a catalog code (auto → zh for server strings)."""
    code = normalize_language(pref)
    if code == "auto":
        return "zh"
    if code.startswith("zh"):
        return "zh" if code == "zh" else "zh"
    if code in _MESSAGES:
        return code
    return "en"


def tr(pref: str, key: str, **params: Any) -> str:
    """Translate ``key`` for the user's language preference."""
    locale = resolve_ui_locale(pref)
    for table_key in (locale, "en", "zh"):
        table = _MESSAGES.get(table_key) or {}
        if key in table:
            text = table[key]
            break
    else:
        text = key
    if params:
        try:
            return text.format(**params)
        except (KeyError, ValueError):
            return text
    return text
