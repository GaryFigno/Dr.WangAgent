"""Claude Code CLI host shell for the GUI Claude panel.

This module is intentionally a thin visualization host:
- spawn the native ``claude`` binary (one process per panel session when concurrent)
- apply profile env/flags (key / base_url / model)
- forward chat text + images
- surface approvals / questions to the UI
- isolate panel sessions so streams never crosstalk

It must not implement its own agent loop, tools, planning, or skills.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.schema import ProviderAccount
from ..process import hidden_subprocess_kwargs
from .claude_profiles import ClaudeProfileStore, _default_efforts_for
from .cli_orphans import KIND_CLAUDE, register_child, reap_orphans, summarize_reaped, unregister_child
from .panel_sessions import PanelSessionStore, PanelTranscriptEntry
from .profile_bridge import account_public, is_kimi_coding

AgentAccountsFn = Callable[[], list[ProviderAccount]]

MAX_LINE_BYTES = 8 * 1024 * 1024
SHUTDOWN_GRACE = 5.0
DATA_URL_RE = re.compile(r"^data:(image/[\w+.-]+);base64,(.+)$", re.DOTALL)

PushFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ParkApprovalFn = Callable[[dict[str, Any]], Awaitable[str | None]]


class ClaudeRuntimeError(Exception):
    """Raised when the Claude Code link fails."""


@dataclass
class ClaudeSlot:
    id: str
    workspace: Path
    session_id: str | None = None
    busy: bool = False
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    buffer: str = ""
    open_tools: dict[int, dict[str, Any]] = field(default_factory=dict)
    tool_by_id: dict[str, str] = field(default_factory=dict)
    stderr_tail: list[str] = field(default_factory=list)
    stopping: bool = False
    state: str = "stopped"  # stopped | starting | ready | error
    last_error: str = ""


def find_claude_executable() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    try:
        home = Path.home()
    except RuntimeError:
        home = Path(".")
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", ""))
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = [
            appdata / "npm" / "claude.cmd",
            appdata / "npm" / "claude.exe",
            local / "Programs" / "claude" / "claude.exe",
            home / ".local" / "bin" / "claude.exe",
        ]
        for path in candidates:
            if path.is_file():
                return str(path)
    else:
        for path in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude"):
            if path.is_file():
                return str(path)
    return None


class ClaudeRuntime:
    """Host shell around native Claude Code CLI processes (one per panel session)."""

    def __init__(
        self,
        *,
        workspace: Path,
        push: PushFn,
        park_approval: ParkApprovalFn,
        executable: str | None = None,
        profiles: ClaudeProfileStore | None = None,
        agent_accounts: AgentAccountsFn | None = None,
        session_store: PanelSessionStore | None = None,
    ):
        self.workspace = workspace
        self._push = push
        self._park_approval = park_approval
        self.profiles = profiles or ClaudeProfileStore()
        self._agent_accounts = agent_accounts
        self.store = session_store or PanelSessionStore("claude")
        self.slots: dict[str, ClaudeSlot] = {}
        self.viewed_id: str = ""
        self.show_archived: bool = False
        self.selection = self.profiles.active_id or "anthropic"
        self._executable = executable
        # VIEWED slot mirrors (kept for back-compat with existing call sites / tests).
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self.stderr_tail: list[str] = []
        self.session_id: str | None = None
        self.busy = False
        self.state = "stopped"
        self.last_error = ""
        self._stopping = False
        self._buffer = ""
        self._temp_files: list[Path] = []
        self._open_tools: dict[int, dict[str, Any]] = {}
        self._tool_by_id: dict[str, str] = {}
        self.selected_model = ""
        self.selected_effort = ""
        self.permission_mode = "ask"  # ask | auto | yolo — aligns with Agent composer
        self.models: list[dict[str, Any]] = []
        profile = self.profiles.get(self.selection)
        if profile and profile.model:
            self.selected_model = profile.model
            self.models = self.profiles.models_for(profile)
            self._sync_effort_for_model(self.selected_model)

    def _capture_viewed_to_slot(self) -> None:
        if not self.viewed_id:
            return
        slot = self.slots.get(self.viewed_id)
        if slot is None:
            return
        slot.workspace = self.workspace
        slot.session_id = self.session_id
        slot.busy = self.busy
        slot.process = self._process
        slot.reader_task = self._reader_task
        slot.stderr_task = self._stderr_task
        slot.write_lock = self._write_lock
        slot.buffer = self._buffer
        slot.open_tools = self._open_tools
        slot.tool_by_id = self._tool_by_id
        slot.stderr_tail = self.stderr_tail
        slot.state = self.state
        slot.last_error = self.last_error
        slot.stopping = self._stopping

    def _apply_slot_to_viewed(self, slot: ClaudeSlot) -> None:
        self.viewed_id = slot.id
        self.workspace = slot.workspace
        self.session_id = slot.session_id
        self.busy = slot.busy
        self._process = slot.process
        self._reader_task = slot.reader_task
        self._stderr_task = slot.stderr_task
        self._write_lock = slot.write_lock
        self._buffer = slot.buffer
        self._open_tools = slot.open_tools
        self._tool_by_id = slot.tool_by_id
        self.stderr_tail = slot.stderr_tail
        self.state = slot.state
        self.last_error = slot.last_error
        self._stopping = slot.stopping

    def _viewed_slot(self) -> ClaudeSlot | None:
        if not self.viewed_id:
            return None
        return self.slots.get(self.viewed_id)

    def _slot_alive(self, slot: ClaudeSlot) -> bool:
        return slot.process is not None and slot.process.returncode is None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _emit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        slot_id: str | None = None,
    ) -> None:
        out = dict(payload)
        sid = (slot_id or self.viewed_id or "").strip()
        if sid:
            out.setdefault("panel_session_id", sid)
        await self._push(kind, out)

    def _running_ids(self) -> list[str]:
        return [slot.id for slot in self.slots.values() if slot.busy]

    def _sessions_ui(self) -> dict[str, Any]:
        return self.store.ui_groups(
            viewed_id=self.viewed_id,
            viewed_workspace=str(self.workspace),
            running_ids=self._running_ids(),
            show_archived=self.show_archived,
        )

    def status_payload(self) -> dict[str, Any]:
        profile = self.profiles.get(self.selection)
        logged_in = bool(profile and self.profiles.is_logged_in(profile))
        model = self.selected_model or (profile.model if profile else "")
        payload: dict[str, Any] = {
            "state": self.state,
            "alive": self.alive,
            "busy": self.busy,
            "session_id": self.session_id or "",
            "workspace": str(self.workspace),
            "error": self.last_error,
            "executable": self._executable or find_claude_executable() or "",
            "selection": self.selection,
            "profile_id": self.selection,
            "model": model,
            "models": list(self.models),
            "selected_model": model,
            "selected_effort": self.selected_effort,
            "effort_levels": self._effort_levels_for(model),
            "permission_mode": self.permission_mode,
            "auth_mode": (profile.auth_mode if profile else "api_key"),
            "logged_in": logged_in,
            "profiles": self.profiles.list_public(),
            "templates": self.profiles.templates_public(),
            "active_profile_id": self.profiles.active_id,
            "agent_accounts": [
                account_public(account)
                for account in (self._agent_accounts() if self._agent_accounts else [])
            ],
            "viewed_id": self.viewed_id,
            "panel_session_id": self.viewed_id,
            "sessions": self._sessions_ui(),
        }
        if self.viewed_id:
            payload["transcript"] = self.store.load_transcript(self.viewed_id)
        return payload

    async def push_status(self) -> None:
        await self._push("claude_status", self.status_payload())

    def _ensure_viewed_meta(self) -> ClaudeSlot:
        from .workspace import is_app_install_workspace, preferred_project_workspace

        if is_app_install_workspace(self.workspace):
            self.workspace = preferred_project_workspace(None)
            for meta in list(self.store.list(include_archived=True)):
                if is_app_install_workspace(meta.workspace) and meta.message_count <= 0:
                    self.store.delete(meta.id)
        if self.viewed_id and self.viewed_id in self.slots:
            return self.slots[self.viewed_id]
        metas = self.store.list(workspace=self.workspace, include_archived=False)
        if metas:
            meta = metas[0]
        else:
            any_metas = self.store.list(include_archived=False)
            if any_metas and not is_app_install_workspace(any_metas[0].workspace):
                meta = any_metas[0]
                self.workspace = Path(meta.workspace)
            else:
                meta = self.store.create(self.workspace, profile_id=self.selection)
        slot = ClaudeSlot(
            id=meta.id,
            workspace=Path(meta.workspace) if meta.workspace else self.workspace,
            session_id=meta.native_id or None,
        )
        self.slots[slot.id] = slot
        self._apply_slot_to_viewed(slot)
        return slot

    def _prepare_profile(self) -> Any | None:
        profile = self.profiles.get(self.selection)
        if profile is None:
            self.profiles.ensure_defaults()
            profile = self.profiles.get(self.profiles.active_id)
        if profile is None:
            return None
        self.selection = profile.id
        self.profiles.active_id = profile.id
        self.profiles.save()
        return profile

    def _live_cli_pids(self, *, exclude_slot: str | None = None) -> set[int]:
        pids: set[int] = set()
        for other in self.slots.values():
            if exclude_slot and other.id == exclude_slot:
                continue
            proc = other.process
            if proc is not None and proc.returncode is None and proc.pid:
                pids.add(int(proc.pid))
        return pids

    async def _reap_orphan_clis(self, *, exclude_slot: str | None = None) -> None:
        """Kill crashed leftovers, then continue with --resume on a fresh process."""
        try:
            killed = await asyncio.to_thread(
                reap_orphans,
                keep_pids=self._live_cli_pids(exclude_slot=exclude_slot),
                kinds=(KIND_CLAUDE,),
            )
        except Exception:  # noqa: BLE001
            return
        tip = summarize_reaped(killed)
        if tip:
            await self._emit("claude_notice", {"level": "info", "text": tip})

    async def _start_slot_process(
        self,
        slot: ClaudeSlot,
        *,
        resume: str | None = None,
    ) -> None:
        """Spawn a CLI process owned by ``slot``. Does not touch other slots."""
        if self._slot_alive(slot) and slot.state == "ready":
            if slot.id == self.viewed_id:
                self._apply_slot_to_viewed(slot)
            return

        await self._reap_orphan_clis(exclude_slot=slot.id)
        await self._stop_slot_process(slot, clear_session=False)

        slot.state = "starting"
        slot.last_error = ""
        if slot.id == self.viewed_id:
            self._apply_slot_to_viewed(slot)
            await self.push_status()

        executable = self._executable or find_claude_executable()
        if not executable:
            slot.state = "error"
            slot.last_error = "未找到 claude 可执行文件。请安装 Claude Code CLI 并确保在 PATH 中。"
            if slot.id == self.viewed_id:
                self._apply_slot_to_viewed(slot)
            await self._emit("claude_error", {"message": slot.last_error}, slot_id=slot.id)
            await self.push_status()
            return

        profile = self._prepare_profile()
        if profile is None:
            slot.state = "error"
            slot.last_error = "没有可用的 Claude Profile"
            if slot.id == self.viewed_id:
                self._apply_slot_to_viewed(slot)
            await self._emit("claude_error", {"message": slot.last_error}, slot_id=slot.id)
            await self.push_status()
            return

        accounts = self._agent_accounts() if self._agent_accounts else None
        has_key = bool(self.profiles.resolve_api_key(profile, agent_accounts=accounts))
        has_login = self.profiles.is_logged_in(profile)
        if profile.auth_mode == "login" and not has_login and not has_key:
            slot.state = "error"
            slot.last_error = (
                "此 Profile 需要先登录 Claude 订阅账号。"
                "请点击「登录」完成浏览器授权，然后再连接。"
            )
            if slot.id == self.viewed_id:
                self._apply_slot_to_viewed(slot)
            await self._emit("claude_error", {"message": slot.last_error}, slot_id=slot.id)
            await self.push_status()
            return
        if profile.auth_mode != "login" and not has_key and not has_login:
            slot.state = "error"
            slot.last_error = (
                "未找到 API Key。请在「档案」中粘贴 ANTHROPIC_API_KEY，"
                "或改用「订阅登录」Profile 并点击「登录」。"
            )
            if slot.id == self.viewed_id:
                self._apply_slot_to_viewed(slot)
            await self._emit("claude_error", {"message": slot.last_error}, slot_id=slot.id)
            await self.push_status()
            return

        notices = self._repair_profile_for_launch(profile)
        self._refresh_models(profile)
        env = self.profiles.launch_env(
            profile,
            agent_accounts=accounts,
            selected_model=self.selected_model,
            selected_effort=self.selected_effort,
        )
        resume_id = (resume or slot.session_id or "").strip()
        if not resume_id:
            meta = self.store.get(slot.id)
            if meta and meta.native_id:
                resume_id = meta.native_id
        args = [
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # Without stdio permission prompts, Claude Code auto-denies Bash in
            # headless mode ("This command requires approval" / user-rejected).
            "--permission-prompt-tool",
            "stdio",
            *self.profiles.launch_args(profile, selected_model=self.selected_model),
            *self._permission_cli_args(),
        ]
        if resume_id:
            args.extend(["--resume", resume_id])
            slot.session_id = resume_id
        try:
            slot.process = await asyncio.create_subprocess_exec(
                executable,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(slot.workspace),
                limit=MAX_LINE_BYTES,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, ValueError) as error:
            slot.state = "error"
            slot.last_error = f"无法启动 Claude Code：{error}"
            if slot.id == self.viewed_id:
                self._apply_slot_to_viewed(slot)
            await self._emit("claude_error", {"message": slot.last_error}, slot_id=slot.id)
            await self.push_status()
            return

        if slot.process and slot.process.pid:
            register_child(
                int(slot.process.pid),
                KIND_CLAUDE,
                command=" ".join([executable, *args]),
            )
        self._executable = executable
        slot.reader_task = asyncio.create_task(self._read_loop(slot.id))
        slot.stderr_task = asyncio.create_task(self._drain_stderr(slot.id))
        slot.state = "ready"
        slot.busy = False
        if slot.id == self.viewed_id:
            self._apply_slot_to_viewed(slot)
        from ..providers import proxy as proxy_mod

        route = proxy_mod.describe_setting(profile.proxy)
        await self._emit(
            "claude_notice",
            {"level": "info", "text": f"Claude Code 已连接（{profile.name} · {route} · 原生 CLI）"},
            slot_id=slot.id,
        )
        for text in notices:
            await self._emit("claude_notice", {"level": "info", "text": text}, slot_id=slot.id)
        await self.push_status()

    async def start(self, resume: str | None = None) -> None:
        slot = self._ensure_viewed_meta()
        if resume:
            slot.session_id = resume
        await self._start_slot_process(slot, resume=resume or slot.session_id)

    async def login(self) -> None:
        """Run native ``claude auth login`` for the active profile's config dir."""
        executable = self._executable or find_claude_executable()
        if not executable:
            await self._emit(
                "claude_error",
                {"message": "未找到 claude 可执行文件，无法登录。"},
            )
            return
        profile = self.profiles.get(self.selection)
        if profile is None:
            self.profiles.ensure_defaults()
            profile = self.profiles.get(self.profiles.active_id)
        if profile is None:
            await self._emit("claude_error", {"message": "没有可用的 Claude Profile"})
            return
        # Prefer login-mode profile; if current is API-key, switch to seeded login.
        if profile.auth_mode != "login":
            login_profile = self.profiles.get("login")
            if login_profile is not None:
                profile = login_profile
                self.selection = profile.id
                self.profiles.set_active(profile.id)
        accounts = self._agent_accounts() if self._agent_accounts else None
        env = self.profiles.launch_env(profile, agent_accounts=accounts)
        await self._emit(
            "claude_notice",
            {
                "level": "info",
                "text": f"正在打开 Claude 登录（{profile.name}）…请在浏览器完成授权后回来点「连接」。",
            },
        )
        # Login needs a visible console / browser; do not hide the window.
        kwargs: dict[str, Any] = {
            "env": env,
            "cwd": str(self.workspace),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                "auth",
                "login",
                **kwargs,
            )
        except (OSError, ValueError) as error:
            await self._emit("claude_error", {"message": f"无法启动登录：{error}"})
            await self.push_status()
            return
        code = await proc.wait()
        if code == 0 or self.profiles.is_logged_in(profile):
            await self._emit(
                "claude_notice",
                {"level": "info", "text": "登录完成。正在连接 Claude Code…"},
            )
            await self.start()
        else:
            await self._emit(
                "claude_notice",
                {
                    "level": "warn",
                    "text": f"登录进程退出码 {code}。若浏览器已授权成功，请直接点「连接」。",
                },
            )
            await self.push_status()

    async def _stop_slot_process(self, slot: ClaudeSlot, *, clear_session: bool = False) -> None:
        slot.stopping = True
        slot.busy = False
        if clear_session:
            slot.session_id = None
        slot.buffer = ""
        slot.open_tools.clear()
        slot.tool_by_id.clear()

        if slot.reader_task is not None:
            slot.reader_task.cancel()
            try:
                await slot.reader_task
            except asyncio.CancelledError:
                pass
            slot.reader_task = None
        if slot.stderr_task is not None:
            slot.stderr_task.cancel()
            try:
                await slot.stderr_task
            except asyncio.CancelledError:
                pass
            slot.stderr_task = None

        if slot.process is not None:
            dead_pid = int(slot.process.pid or 0)
            if slot.process.returncode is None:
                try:
                    if slot.process.stdin is not None:
                        slot.process.stdin.close()
                    await asyncio.wait_for(slot.process.wait(), timeout=SHUTDOWN_GRACE)
                except (asyncio.TimeoutError, ProcessLookupError, ValueError):
                    try:
                        slot.process.kill()
                        await slot.process.wait()
                    except (ProcessLookupError, ValueError):
                        pass
            unregister_child(dead_pid)
            slot.process = None

        slot.state = "stopped"
        slot.stopping = False
        if slot.id == self.viewed_id:
            self._apply_slot_to_viewed(slot)

    async def stop(self, *, clear_sessions: bool = False) -> None:
        """Disconnect all Claude panel processes.

        By default keep native session ids for resume. Pass ``clear_sessions=True``
        when switching providers — a Kimi session id is not valid under DeepSeek.
        """
        self._capture_viewed_to_slot()
        self._cleanup_temps()
        for slot in list(self.slots.values()):
            await self._stop_slot_process(slot, clear_session=clear_sessions)
            try:
                if clear_sessions:
                    self.store.touch(slot.id, native_id="")
                elif slot.session_id:
                    self.store.touch(slot.id, native_id=slot.session_id)
            except Exception:  # noqa: BLE001
                pass
        if self.viewed_id and self.viewed_id in self.slots:
            self._apply_slot_to_viewed(self.slots[self.viewed_id])
        else:
            self.state = "stopped"
            self.busy = False
            self._process = None
        await self.push_status()

    async def set_selection(self, selection: str) -> None:
        next_sel = (selection or "").strip() or self.profiles.active_id or "anthropic"
        if next_sel == self.selection and self.alive and self.state == "ready":
            await self.push_status()
            return
        if self.profiles.get(next_sel) is None:
            raise ValueError(f"unknown Claude profile '{next_sel}'")
        prev = self.selection
        self.selection = next_sel
        self.profiles.set_active(next_sel)
        self.selected_model = ""
        self.selected_effort = ""
        self.models = []
        profile = self.profiles.get(next_sel)
        if profile and profile.model:
            self.selected_model = profile.model
            self.models = self.profiles.models_for(profile)
            self._sync_effort_for_model(self.selected_model)
        # Provider-bound resume ids must not cross profiles.
        await self.stop(clear_sessions=(prev != next_sel))
        await self.start()

    def _repair_profile_for_launch(self, profile: Any) -> list[str]:
        """Auto-fix known bad URLs on connect; return user-facing notices."""
        notices: list[str] = []
        before = profile.base_url
        fixed = self.profiles.repair_profiles()
        # Re-read after store-wide repair.
        current = self.profiles.get(profile.id) or profile
        url = (current.base_url or "").lower()
        if "deepseek.com" in url and "/anthropic" not in url:
            current.base_url = "https://api.deepseek.com/anthropic"
            if current.template not in {"deepseek"}:
                current.template = "deepseek"
            if not current.model or str(current.model).startswith("claude"):
                current.model = "deepseek-v4-flash"
            self.profiles.save()
            notices.append("已自动修复 DeepSeek URL：/v1 → /anthropic")
        elif before != current.base_url and "deepseek.com" in (current.base_url or "").lower():
            notices.append("已自动修复 DeepSeek URL：/v1 → /anthropic")
        if is_kimi_coding(current.base_url) or (
            "api.kimi.com" in (current.base_url or "").lower()
            and "coding" in (current.base_url or "").lower()
        ):
            if before.rstrip("/") != current.base_url.rstrip("/"):
                notices.append("已自动修复 Kimi Coding Anthropic URL（去掉 /v1）")
            from .claude_profiles import _map_kimi_claude_model

            old_sel = self.selected_model or current.model or ""
            mapped = _map_kimi_claude_model(old_sel)
            if mapped and mapped != self.selected_model:
                if self.selected_model and self.selected_model != mapped:
                    notices.append(f"已自动修正模型：{self.selected_model} → {mapped}")
                self.selected_model = mapped
            if current.model and current.model != mapped:
                current.model = mapped
                self.profiles.save()
        if fixed and not notices:
            notices.append(f"已自动修复 {fixed} 个 Claude Profile 配置")
        # Keep runtime selection in sync.
        if current.model and not self.selected_model:
            self.selected_model = current.model
        return notices

    def _refresh_models(self, profile: Any) -> None:
        self.models = self.profiles.models_for(profile)
        if not self.selected_model:
            self.selected_model = profile.model or (
                str(self.models[0]["id"]) if self.models else ""
            )
        self._sync_effort_for_model(self.selected_model)

    def _effort_levels_for(self, model_id: str) -> list[str]:
        for item in self.models:
            if item.get("id") == model_id:
                levels = item.get("efforts") or []
                return [str(level) for level in levels if level]
        profile = self.profiles.get(self.selection)
        template = profile.template if profile else ""
        return _default_efforts_for(template, model_id)

    def _sync_effort_for_model(self, model_id: str) -> None:
        levels = self._effort_levels_for(model_id)
        if not levels:
            self.selected_effort = ""
            return
        if self.selected_effort in levels:
            return
        for item in self.models:
            if item.get("id") == model_id and item.get("default_effort") in levels:
                self.selected_effort = str(item["default_effort"])
                return
        self.selected_effort = levels[-1] if levels else ""

    async def set_model(self, model_id: str) -> None:
        model_id = (model_id or "").strip()
        if not model_id:
            return
        self.selected_model = model_id
        profile = self.profiles.get(self.selection)
        if profile is not None:
            profile.model = model_id
            self.profiles.save()
            self.models = self.profiles.models_for(profile)
        self._sync_effort_for_model(model_id)
        await self.push_status()
        await self._emit(
            "claude_notice",
            {"level": "info", "text": f"已选择模型：{model_id}（重新连接后生效）"},
        )
        if self.alive and self.state == "ready":
            await self.stop()
            await self.start()

    async def set_effort(self, effort: str) -> None:
        effort = (effort or "").strip()
        levels = self._effort_levels_for(self.selected_model or "")
        if effort and levels and effort not in levels:
            await self._emit(
                "claude_notice",
                {"level": "warn", "text": f"当前模型不支持 effort={effort}"},
            )
            return
        self.selected_effort = effort
        await self.push_status()
        await self._emit(
            "claude_notice",
            {
                "level": "info",
                "text": f"已选择 effort：{effort or '默认'}（重新连接后生效）",
            },
        )
        if self.alive and self.state == "ready":
            await self.stop()
            await self.start()

    def _permission_cli_args(self) -> list[str]:
        """Map ask/auto/yolo onto Claude Code ``--permission-mode``."""
        mapping = {
            "ask": "default",
            "auto": "acceptEdits",
            "yolo": "bypassPermissions",
        }
        native = mapping.get(self.permission_mode, "default")
        args = ["--permission-mode", native]
        # Headless print mode needs the explicit allow flag for bypass.
        if native == "bypassPermissions":
            args.append("--allow-dangerously-skip-permissions")
        return args

    async def set_permission_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode not in {"ask", "auto", "yolo"}:
            await self._emit(
                "claude_notice",
                {"level": "warn", "text": f"未知权限模式：{mode}"},
            )
            return
        prev = self.permission_mode
        self.permission_mode = mode
        await self.push_status()
        if self.alive and self.state == "ready" and prev != mode:
            await self._emit(
                "claude_notice",
                {"level": "info", "text": f"权限模式：{mode}（正在重新连接…）"},
            )
            await self.stop()
            await self.start()
            await self._emit(
                "claude_notice",
                {"level": "info", "text": f"权限模式已生效：{mode}"},
            )
        else:
            await self._emit(
                "claude_notice",
                {"level": "info", "text": f"权限模式：{mode}"},
            )

    async def upsert_profile(self, args: dict[str, Any]) -> None:
        template = str(args.get("template") or "anthropic")
        auth_mode = str(args.get("auth_mode") or "")
        if not auth_mode and template == "login":
            auth_mode = "login"
        profile = self.profiles.upsert(
            profile_id=str(args.get("id") or ""),
            name=str(args.get("name") or ""),
            env_key=str(args.get("env_key") or ""),
            base_url=str(args.get("base_url") or ""),
            model=str(args.get("model") or ""),
            template=template,
            auth_mode=auth_mode,
            api_key=str(args.get("api_key") or ""),
            proxy=str(args.get("proxy") or ""),
            note=str(args.get("note") or ""),
            make_active=bool(args.get("make_active")),
        )
        await self._emit(
            "claude_notice",
            {"level": "info", "text": f"已保存 Claude Profile：{profile.name}"},
        )
        if args.get("activate") or args.get("make_active"):
            await self.set_selection(profile.id)
        else:
            await self.push_status()

    async def delete_profile(self, profile_id: str) -> None:
        if not self.profiles.delete(profile_id):
            raise ValueError(f"unknown Claude profile '{profile_id}'")
        if self.selection == profile_id:
            await self.set_selection(self.profiles.active_id or "anthropic")
        else:
            await self.push_status()

    async def import_account(self, account_id: str, *, activate: bool = True) -> None:
        accounts = self._agent_accounts() if self._agent_accounts else []
        account = next((a for a in accounts if a.id == account_id), None)
        if account is None:
            raise ValueError(f"unknown Agent account '{account_id}'")
        profile = self.profiles.import_from_account(account, make_active=activate)
        await self._emit(
            "claude_notice",
            {"level": "info", "text": f"已从 Agent 导入 Profile：{profile.name}"},
        )
        if activate:
            await self.set_selection(profile.id)
        else:
            await self.push_status()

    async def new_session(self, workspace: Path | None = None) -> None:
        self._capture_viewed_to_slot()
        ws = Path(workspace) if workspace is not None else self.workspace
        meta = self.store.create(ws, profile_id=self.selection)
        slot = ClaudeSlot(id=meta.id, workspace=ws)
        self.slots[slot.id] = slot
        self._apply_slot_to_viewed(slot)
        await self._start_slot_process(slot, resume=None)
        await self.push_status()

    async def open_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        if sid == self.viewed_id:
            await self.push_status()
            return
        meta = self.store.get(sid)
        if meta is None:
            await self._emit("claude_error", {"message": "没有这个 Claude 会话"})
            return
        self._capture_viewed_to_slot()
        slot = self.slots.get(sid)
        if slot is None:
            slot = ClaudeSlot(
                id=meta.id,
                workspace=Path(meta.workspace) if meta.workspace else self.workspace,
                session_id=meta.native_id or None,
            )
            self.slots[sid] = slot
        self._apply_slot_to_viewed(slot)
        if not self._slot_alive(slot):
            await self._start_slot_process(slot, resume=slot.session_id or meta.native_id or None)
        else:
            await self.push_status()

    async def delete_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        slot = self.slots.pop(sid, None)
        if slot is not None:
            await self._stop_slot_process(slot, clear_session=True)
        self.store.delete(sid)
        if sid == self.viewed_id:
            self.viewed_id = ""
            remaining = self.store.list(workspace=self.workspace, include_archived=False)
            if remaining:
                await self.open_session(remaining[0].id)
                return
            await self.new_session(self.workspace)
            return
        await self.push_status()

    async def archive_session(self, session_id: str, archived: bool) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        if self.store.set_archived(sid, archived) is None:
            await self._emit("claude_error", {"message": "没有这个 Claude 会话"})
            return
        if archived and sid == self.viewed_id:
            remaining = self.store.list(workspace=self.workspace, include_archived=False)
            if remaining:
                await self.open_session(remaining[0].id)
                return
            await self.new_session(self.workspace)
            return
        await self.push_status()

    async def set_panel_workspace(self, workspace: Path) -> None:
        """Change workspace for the viewed slot only."""
        self._capture_viewed_to_slot()
        slot = self._viewed_slot()
        if slot is None:
            slot = self._ensure_viewed_meta()
        slot.workspace = workspace
        self.workspace = workspace
        # New cwd → fresh native session for this slot; stop+restart without resume.
        await self._stop_slot_process(slot, clear_session=True)
        self.store.touch(slot.id, workspace=workspace, native_id="")
        await self._start_slot_process(slot, resume=None)
        await self.push_status()

    async def set_workspace(self, workspace: Path) -> None:
        await self.set_panel_workspace(workspace)

    async def forget_workspace(self, workspace: Path | str) -> None:
        want = str(workspace)
        for sid, slot in list(self.slots.items()):
            if str(slot.workspace) == want:
                await self._stop_slot_process(slot, clear_session=True)
                self.slots.pop(sid, None)
        self.store.delete_workspace(workspace)
        if str(self.workspace) == want or (self.viewed_id and self.viewed_id not in self.slots):
            self.viewed_id = ""
            remaining = self.store.list(include_archived=False)
            if remaining:
                await self.open_session(remaining[0].id)
            else:
                await self.new_session(self.workspace if Path(self.workspace).is_dir() else Path.cwd())
            return
        await self.push_status()

    async def set_show_archived(self, show: bool) -> None:
        self.show_archived = bool(show)
        await self.push_status()

    async def prompt(self, text: str, images: list[dict[str, Any]] | None = None) -> None:
        text = (text or "").strip()
        images = list(images or [])
        if not text and not images:
            return
        slot = self._viewed_slot() or self._ensure_viewed_meta()
        if not self._slot_alive(slot) or slot.state != "ready":
            await self._start_slot_process(slot, resume=slot.session_id)
        if not self._slot_alive(slot) or slot.state != "ready":
            await self._emit(
                "claude_error",
                {"message": slot.last_error or "Claude Code 未就绪"},
                slot_id=slot.id,
            )
            return
        if slot.busy:
            await self._emit(
                "claude_notice",
                {"level": "warn", "text": "Claude Code 正在处理上一轮，请稍候或中断"},
                slot_id=slot.id,
            )
            return
        slot.busy = True
        slot.buffer = ""
        slot.open_tools.clear()
        if slot.id == self.viewed_id:
            self.busy = True
            self._buffer = ""
            self._open_tools = slot.open_tools
        if text:
            try:
                self.store.append_transcript(
                    slot.id,
                    PanelTranscriptEntry(role="user", text=text),
                )
                meta = self.store.get(slot.id)
                if meta and (not meta.title or meta.title == "新对话"):
                    self.store.touch(slot.id, title=text[:40])
            except Exception:  # noqa: BLE001
                pass
        await self._emit(
            "claude_activity",
            {"text": "Claude Code 正在处理…", "kind": "busy"},
            slot_id=slot.id,
        )
        await self.push_status()
        content = _build_user_content(text, images)
        message = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": slot.session_id or str(uuid.uuid4()),
        }
        if not slot.session_id:
            slot.session_id = str(message["session_id"])
            if slot.id == self.viewed_id:
                self.session_id = slot.session_id
            try:
                self.store.touch(slot.id, native_id=slot.session_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._write_slot(slot, message)
        except ClaudeRuntimeError as error:
            slot.busy = False
            if slot.id == self.viewed_id:
                self.busy = False
            slot.last_error = str(error)
            await self._emit("claude_error", {"message": slot.last_error}, slot_id=slot.id)
            await self._emit("claude_done", {"ok": False}, slot_id=slot.id)
            await self.push_status()

    async def interrupt(self) -> None:
        """Interrupt the viewed slot only; preserve native session_id and resume."""
        slot = self._viewed_slot()
        if slot is None or not self._slot_alive(slot):
            return
        native = slot.session_id or ""
        meta = self.store.get(slot.id)
        if meta and meta.native_id:
            native = meta.native_id or native
        try:
            await self._write_slot(
                slot,
                {
                    "type": "control_request",
                    "request": {"subtype": "interrupt"},
                    "request_id": uuid.uuid4().hex[:8],
                },
            )
        except ClaudeRuntimeError:
            pass
        await self._emit(
            "claude_notice",
            {"level": "warn", "text": "正在中断 Claude Code…"},
            slot_id=slot.id,
        )
        # Preserve native session across stop+restart (fixes prior clear bug).
        if native:
            slot.session_id = native
            try:
                self.store.touch(slot.id, native_id=native)
            except Exception:  # noqa: BLE001
                pass
        await self._stop_slot_process(slot, clear_session=False)
        await self._start_slot_process(slot, resume=native or None)
        await self._emit("claude_done", {"ok": False}, slot_id=slot.id)

    async def _write_slot(self, slot: ClaudeSlot, payload: dict[str, Any]) -> None:
        if slot.process is None or slot.process.stdin is None:
            raise ClaudeRuntimeError("Claude Code is not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with slot.write_lock:
            slot.process.stdin.write(line.encode("utf-8"))
            await slot.process.stdin.drain()

    async def _write(self, payload: dict[str, Any]) -> None:
        """Write to the viewed slot (back-compat)."""
        slot = self._viewed_slot()
        if slot is None:
            raise ClaudeRuntimeError("Claude Code is not running")
        await self._write_slot(slot, payload)

    async def _read_loop(self, slot_id: str) -> None:
        slot = self.slots.get(slot_id)
        if slot is None or slot.process is None or slot.process.stdout is None:
            return
        try:
            while True:
                raw = await slot.process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message, slot_id=slot_id)
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001
            slot = self.slots.get(slot_id)
            if slot is not None:
                slot.last_error = f"Claude Code 读取失败：{error}"
                if slot_id == self.viewed_id:
                    self.last_error = slot.last_error
            await self._emit(
                "claude_error",
                {"message": f"Claude Code 读取失败：{error}"},
                slot_id=slot_id,
            )
        finally:
            slot = self.slots.get(slot_id)
            if slot is not None:
                if not slot.stopping and slot.state in {"ready", "starting"}:
                    slot.state = "error"
                    code = None
                    if slot.process is not None:
                        code = slot.process.returncode
                    stderr = "\n".join(slot.stderr_tail[-12:]).strip()
                    base = slot.last_error or "Claude Code 已退出"
                    if code is not None:
                        base = f"{base}（exit {code}）"
                    if stderr:
                        # Keep the toast readable; full tail stays in stderr_tail.
                        snippet = stderr.replace("\n", " · ")
                        if len(snippet) > 360:
                            snippet = snippet[:357] + "…"
                        base = f"{base}：{snippet}"
                    slot.last_error = base
                    await self._emit(
                        "claude_error",
                        {"message": slot.last_error, "stderr": stderr, "exit_code": code},
                        slot_id=slot_id,
                    )
                    if stderr:
                        await self._emit(
                            "claude_notice",
                            {"level": "error", "text": f"Claude stderr：{stderr[:800]}"},
                            slot_id=slot_id,
                        )
                slot.busy = False
                if slot_id == self.viewed_id:
                    self._apply_slot_to_viewed(slot)
                if not slot.stopping:
                    await self.push_status()

    async def _dispatch(self, message: dict[str, Any], *, slot_id: str = "") -> None:
        sid = slot_id or self.viewed_id
        slot = self.slots.get(sid) if sid else self._viewed_slot()
        if slot is None:
            return
        kind = str(message.get("type") or "")
        if message.get("session_id"):
            slot.session_id = str(message["session_id"])
            if sid == self.viewed_id:
                self.session_id = slot.session_id
            try:
                self.store.touch(sid, native_id=slot.session_id)
            except Exception:  # noqa: BLE001
                pass

        if kind == "stream_event":
            await self._dispatch_stream_event(message, slot=slot)
            return

        if kind == "assistant":
            await self._dispatch_assistant_message(message, slot=slot)
            return

        if kind == "result":
            slot.busy = False
            if sid == self.viewed_id:
                self.busy = False
            if not slot.buffer:
                final = message.get("result")
                if isinstance(final, str) and final:
                    await self._emit("claude_text", {"delta": final}, slot_id=sid)
                    try:
                        self.store.append_transcript(
                            sid,
                            PanelTranscriptEntry(role="assistant", text=final),
                        )
                    except Exception:  # noqa: BLE001
                        pass
            elif slot.buffer:
                try:
                    self.store.append_transcript(
                        sid,
                        PanelTranscriptEntry(role="assistant", text=slot.buffer),
                    )
                except Exception:  # noqa: BLE001
                    pass
            for call_id in list(slot.tool_by_id):
                await self._emit(
                    "claude_tool_end",
                    {
                        "call_id": call_id,
                        "name": slot.tool_by_id.get(call_id, "tool"),
                        "summary": slot.tool_by_id.get(call_id, "tool"),
                        "content": "",
                        "is_error": False,
                        "duration": 0.0,
                    },
                    slot_id=sid,
                )
            slot.open_tools.clear()
            slot.tool_by_id.clear()
            await self._emit("claude_activity", {"text": "", "kind": "clear"}, slot_id=sid)
            await self._emit("claude_done", {"ok": not bool(message.get("is_error"))}, slot_id=sid)
            await self.push_status()
            return

        if kind in {"control_request", "control"}:
            await self._handle_control(message, slot=slot)
            return

        if kind == "user":
            await self._dispatch_user_tool_results(message, slot=slot)
            return

        if kind == "system":
            text = _extract_text(message)
            if text:
                await self._emit("claude_notice", {"level": "info", "text": text}, slot_id=sid)
            subtype = str(message.get("subtype") or "")
            if subtype == "api_retry":
                detail = (
                    message.get("error")
                    or message.get("message")
                    or message.get("reason")
                    or ""
                )
                tip = str(detail).strip() or "上游 API 暂时失败，Claude Code 正在重试"
                await self._emit(
                    "claude_notice",
                    {"level": "warn", "text": f"API 重试：{tip}"},
                    slot_id=sid,
                )
                await self._emit(
                    "claude_activity",
                    {"text": "API 重试中…", "kind": "busy"},
                    slot_id=sid,
                )
                return
            # Periodic status/init heartbeats must not leave a pulsing "系统: status"
            # after the turn is idle. Only surface meaningful subtypes.
            if subtype in {"", "status", "init", "compact_boundary", "task_started", "task_notification"}:
                if not slot.busy:
                    await self._emit(
                        "claude_activity",
                        {"text": "", "kind": "clear"},
                        slot_id=sid,
                    )
                return
            if subtype:
                await self._emit(
                    "claude_activity",
                    {"text": f"系统：{subtype}", "kind": "busy"},
                    slot_id=sid,
                )
            return

        if kind == "error" or message.get("is_error"):
            err = message.get("error") or message.get("result") or message
            await self._emit("claude_notice", {"level": "error", "text": str(err)}, slot_id=sid)

    async def _dispatch_stream_event(self, message: dict[str, Any], *, slot: ClaudeSlot) -> None:
        sid = slot.id
        event = message.get("event") if isinstance(message.get("event"), dict) else {}
        etype = str(event.get("type") or "")
        if etype == "content_block_start":
            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            index = event.get("index")
            btype = str(block.get("type") or "")
            if btype == "tool_use":
                call_id = str(block.get("id") or f"tool-{index}")
                name = str(block.get("name") or "tool")
                try:
                    idx = int(index)
                except (TypeError, ValueError):
                    idx = -1
                if idx >= 0:
                    slot.open_tools[idx] = {"call_id": call_id, "name": name}
                slot.tool_by_id[call_id] = name
                await self._emit(
                    "claude_activity",
                    {"text": f"运行 {name}…", "kind": "busy"},
                    slot_id=sid,
                )
                await self._emit(
                    "claude_tool_start",
                    {
                        "call_id": call_id,
                        "name": name,
                        "headline": name,
                        "args": block.get("input") if isinstance(block.get("input"), dict) else {},
                    },
                    slot_id=sid,
                )
                return
            if btype == "thinking":
                await self._emit("claude_activity", {"text": "思考中…", "kind": "thinking"}, slot_id=sid)
                return
            if btype == "text":
                await self._emit("claude_activity", {"text": "回答中…", "kind": "streaming"}, slot_id=sid)
                return
            return

        if etype == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            dtype = str(delta.get("type") or "")
            if dtype in {"thinking_delta", "thinking"} or delta.get("thinking"):
                text = str(delta.get("thinking") or delta.get("text") or "")
                if text:
                    await self._emit("claude_activity", {"text": "思考中…", "kind": "thinking"}, slot_id=sid)
                    await self._emit("claude_thinking", {"delta": text}, slot_id=sid)
                return
            if dtype in {"text_delta", "text"} or delta.get("text"):
                text = str(delta.get("text") or "")
                if text:
                    slot.buffer += text
                    if sid == self.viewed_id:
                        self._buffer = slot.buffer
                    await self._emit("claude_activity", {"text": "回答中…", "kind": "streaming"}, slot_id=sid)
                    await self._emit("claude_text", {"delta": text}, slot_id=sid)
                return
            return

        if etype == "content_block_stop":
            try:
                idx = int(event.get("index"))
            except (TypeError, ValueError):
                return
            meta = slot.open_tools.pop(idx, None)
            if not meta:
                return
            name = str(meta.get("name") or "tool")
            await self._emit(
                "claude_activity",
                {"text": f"{name} 执行中…", "kind": "busy"},
                slot_id=sid,
            )
            return

        text = _extract_text(message)
        if text:
            slot.buffer += text
            if sid == self.viewed_id:
                self._buffer = slot.buffer
            await self._emit("claude_text", {"delta": text}, slot_id=sid)

    async def _dispatch_assistant_message(self, message: dict[str, Any], *, slot: ClaudeSlot) -> None:
        sid = slot.id
        nested = message.get("message") if isinstance(message.get("message"), dict) else {}
        content = nested.get("content") if isinstance(nested, dict) else message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "")
                if btype == "text":
                    if slot.buffer:
                        continue
                    text = str(block.get("text") or "")
                    if text:
                        slot.buffer += text
                        if sid == self.viewed_id:
                            self._buffer = slot.buffer
                        await self._emit("claude_text", {"delta": text}, slot_id=sid)
                elif btype == "tool_use":
                    call_id = str(block.get("id") or "")
                    name = str(block.get("name") or "tool")
                    if call_id and call_id not in slot.tool_by_id:
                        slot.tool_by_id[call_id] = name
                        await self._emit(
                            "claude_activity",
                            {"text": f"运行 {name}…", "kind": "busy"},
                            slot_id=sid,
                        )
                        await self._emit(
                            "claude_tool_start",
                            {
                                "call_id": call_id,
                                "name": name,
                                "headline": name,
                                "args": block.get("input")
                                if isinstance(block.get("input"), dict)
                                else {},
                            },
                            slot_id=sid,
                        )
                elif btype == "thinking":
                    thinking = str(block.get("thinking") or block.get("text") or "")
                    if thinking:
                        await self._emit("claude_thinking", {"delta": thinking}, slot_id=sid)
            return
        text = _extract_text(message)
        if text:
            slot.buffer += text
            if sid == self.viewed_id:
                self._buffer = slot.buffer
            await self._emit("claude_text", {"delta": text}, slot_id=sid)

    async def _dispatch_user_tool_results(self, message: dict[str, Any], *, slot: ClaudeSlot) -> None:
        sid = slot.id
        nested = message.get("message") if isinstance(message.get("message"), dict) else {}
        content = nested.get("content") if isinstance(nested, dict) else message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or block.get("tool_useId") or "")
            if not call_id:
                continue
            name = slot.tool_by_id.pop(call_id, "tool")
            is_error = bool(block.get("is_error"))
            raw = block.get("content")
            if isinstance(raw, list):
                parts = []
                for part in raw:
                    if isinstance(part, dict) and part.get("text"):
                        parts.append(str(part["text"]))
                    elif isinstance(part, str):
                        parts.append(part)
                content_text = "\n".join(parts)
            else:
                content_text = str(raw or "")
            await self._emit(
                "claude_tool_end",
                {
                    "call_id": call_id,
                    "name": name,
                    "summary": name,
                    "content": content_text[:8000],
                    "is_error": is_error,
                    "duration": 0.0,
                },
                slot_id=sid,
            )
            await self._emit(
                "claude_activity",
                {"text": f"{'失败' if is_error else '完成'} {name}", "kind": "error" if is_error else "busy"},
                slot_id=sid,
            )

    async def _handle_control(self, message: dict[str, Any], *, slot: ClaudeSlot) -> None:
        request_id = message.get("request_id") or message.get("id") or uuid.uuid4().hex[:8]
        request = message.get("request") if isinstance(message.get("request"), dict) else {}
        subtype = str(
            message.get("subtype")
            or (request.get("subtype") if isinstance(request, dict) else "")
            or ""
        )
        params = request or message
        detail = json.dumps(params, ensure_ascii=False)[:1200]
        tool_name = str(
            params.get("tool_name")
            or params.get("tool")
            or (request.get("tool_name") if isinstance(request, dict) else "")
            or "tool"
        )
        tool_input = params.get("input")
        if tool_input is None and isinstance(request, dict):
            tool_input = request.get("input")
        if self.permission_mode in {"auto", "yolo"}:
            answer = "acceptForSession" if self.permission_mode == "yolo" else "accept"
        else:
            answer = await self._park_approval(
                {
                    "kind": "control",
                    "tool": f"Claude Code · {tool_name}",
                    "reason": f"Claude Code 请求确认（{subtype or 'permission'}）",
                    "detail": detail,
                    "params": params,
                    "panel_session_id": slot.id,
                }
            )
        allowed = str(answer or "").lower() in {
            "accept",
            "once",
            "allow",
            "yes",
            "approve",
            "acceptforsession",
            "always",
        }
        # Claude Code requires updatedInput on allow; omitting it is treated as reject.
        decision: dict[str, Any]
        if allowed:
            decision = {"behavior": "allow"}
            if isinstance(tool_input, dict):
                decision["updatedInput"] = tool_input
            elif tool_input is not None:
                decision["updatedInput"] = tool_input
        else:
            decision = {
                "behavior": "deny",
                "message": "User declined this tool use",
            }
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success" if allowed else "error",
                "request_id": request_id,
                "response": decision,
            },
        }
        try:
            await self._write_slot(slot, response)
            if allowed and self.permission_mode in {"auto", "yolo"}:
                await self._emit(
                    "claude_activity",
                    {"text": f"已自动批准 {tool_name}", "kind": "busy"},
                    slot_id=slot.id,
                )
        except ClaudeRuntimeError as error:
            await self._emit("claude_notice", {"level": "error", "text": str(error)}, slot_id=slot.id)

    async def _drain_stderr(self, slot_id: str) -> None:
        slot = self.slots.get(slot_id)
        if slot is None or slot.process is None or slot.process.stderr is None:
            return
        try:
            async for raw in slot.process.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    slot.stderr_tail.append(line)
                    del slot.stderr_tail[:-40]
                    if slot_id == self.viewed_id:
                        self.stderr_tail = slot.stderr_tail
        except (asyncio.CancelledError, ValueError):
            return

    def _cleanup_temps(self) -> None:
        for path in self._temp_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._temp_files.clear()


def _build_user_content(text: str, images: list[dict[str, Any]]) -> Any:
    """Build Claude-native user content (text + image blocks)."""
    if not images:
        return text or ""
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for image in images:
        block = _image_block(image)
        if block:
            blocks.append(block)
    return blocks or (text or "")


def _image_block(image: dict[str, Any]) -> dict[str, Any] | None:
    data_url = str(image.get("data_url") or image.get("url") or "")
    path = str(image.get("path") or "")
    mime = str(image.get("mime") or image.get("media_type") or "image/png")
    if data_url.startswith("data:"):
        match = DATA_URL_RE.match(data_url)
        if not match:
            return None
        mime = match.group(1)
        data = match.group(2).replace("\n", "")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": data},
        }
    if path and Path(path).is_file():
        raw = Path(path).read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        suffix = Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif suffix == ".webp":
            mime = "image/webp"
        elif suffix == ".gif":
            mime = "image/gif"
        else:
            mime = "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        }
    if data_url:
        # Remote URL if Claude Code accepts it.
        return {"type": "image", "source": {"type": "url", "url": data_url}}
    return None


def _extract_text(message: dict[str, Any]) -> str:
    """Pull visible assistant text from stream-json message shapes."""
    if message.get("type") == "stream_event":
        event = message.get("event") or {}
        if isinstance(event, dict):
            # Never treat tool-arg partial_json as chat text.
            if event.get("type") == "content_block_delta":
                d = event.get("delta") or {}
                if isinstance(d, dict):
                    dtype = str(d.get("type") or "")
                    if dtype in {"input_json_delta", "input_json"} or d.get("partial_json"):
                        return ""
                    if d.get("text"):
                        return str(d["text"])
            delta = event.get("delta") or {}
            if isinstance(delta, dict) and delta.get("text"):
                return str(delta["text"])
        return ""

    content = None
    nested = message.get("message")
    if isinstance(nested, dict):
        content = nested.get("content")
    if content is None:
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""
