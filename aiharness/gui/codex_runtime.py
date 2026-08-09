"""Codex app-server host shell for the GUI Codex panel.

Thin visualization host only:
- spawn native ``codex app-server``
- apply provider profile → CODEX_HOME / env
- forward chat text + images
- surface native approvals / questions
- isolate multiple panel sessions (threads) without crosstalk

Does not implement its own agent loop, tools, planning, or skills.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config.schema import ProviderAccount
from ..process import hidden_subprocess_kwargs
from ..providers import proxy as proxy_mod
from .cli_orphans import KIND_CODEX, register_child, reap_orphans, summarize_reaped, unregister_child
from .codex_profiles import KNOWN_PROVIDER_MODELS, CodexProfileStore, ensure_kimi_home_compat
from .panel_sessions import PanelSessionStore, PanelTranscriptEntry
from .profile_bridge import account_public
from .responses_bridge import ResponsesBridge, needs_responses_bridge

AgentAccountsFn = Callable[[], list[ProviderAccount]]

MAX_LINE_BYTES = 8 * 1024 * 1024
SHUTDOWN_GRACE = 5.0
REQUEST_TIMEOUT = 120.0

HOME_KIMI = "kimi"  # legacy alias → active / kimi profile
HOME_DEFAULT = "default"

PushFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ParkApprovalFn = Callable[[dict[str, Any]], Awaitable[str | None]]


class CodexRuntimeError(Exception):
    """Raised when the Codex app-server link fails."""


@dataclass
class CodexSlot:
    id: str
    workspace: Path
    thread_id: str | None = None
    turn_id: str | None = None
    busy: bool = False
    #: True once we saw streaming agentMessage deltas this turn (avoid duplicate full text).
    streamed_text: bool = False
    #: Emit "回答中/思考中" activity at most once per turn (deltas are high-frequency).
    activity_streaming: bool = False
    activity_thinking: bool = False


def default_home_path() -> Path:
    return Path.home() / ".codex"


def kimi_home_path() -> Path:
    return Path.home() / ".codex-kimi"


def resolve_home(kind: str) -> Path:
    if kind == HOME_DEFAULT:
        return default_home_path()
    return kimi_home_path()


def ensure_kimi_home(path: Path | None = None) -> Path:
    """Back-compat wrapper used by older tests."""
    return ensure_kimi_home_compat(path)


def find_codex_executable() -> str | None:
    """Locate the Codex CLI binary."""
    found = shutil.which("codex")
    if found:
        return found
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if local.is_dir():
            candidates = sorted(local.glob("*/codex.exe"), reverse=True)
            if candidates:
                return str(candidates[0])
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        if pf.is_dir():
            try:
                for path in pf.glob("OpenAI.Codex_*\\app\\resources\\codex.exe"):
                    return str(path)
            except OSError:
                pass
    return None


class CodexRuntime:
    """One ``codex app-server`` child process hosting many panel session threads."""

    def __init__(
        self,
        *,
        workspace: Path,
        push: PushFn,
        park_approval: ParkApprovalFn,
        home_kind: str = HOME_KIMI,
        executable: str | None = None,
        profiles: CodexProfileStore | None = None,
        agent_accounts: AgentAccountsFn | None = None,
        session_store: PanelSessionStore | None = None,
    ):
        self.workspace = workspace
        self._push = push
        self._park_approval = park_approval
        self.profiles = profiles or CodexProfileStore()
        self._agent_accounts = agent_accounts
        self.store = session_store or PanelSessionStore("codex")
        self.slots: dict[str, CodexSlot] = {}
        self.viewed_id: str = ""
        self._thread_index: dict[str, str] = {}
        self.show_archived: bool = False
        # selection: "default" (system ~/.codex) or a profile id
        if home_kind == HOME_DEFAULT:
            self.selection = HOME_DEFAULT
        elif home_kind == HOME_KIMI:
            self.selection = self.profiles.active_id or "kimi"
        else:
            self.selection = home_kind
        self._executable = executable
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[Any, asyncio.Future] = {}
        self._next_id = 1
        self.stderr_tail: list[str] = []
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.busy = False
        self.state = "stopped"  # stopped | starting | ready | error
        self.last_error = ""
        self.model = ""
        self.model_provider = ""
        self.models: list[dict[str, Any]] = []
        self.selected_model = ""
        self.selected_effort = ""
        self.permission_mode = "ask"  # ask | auto | yolo — aligns with Agent composer
        self._stopping = False
        self._responses_bridge: ResponsesBridge | None = None
        self._bridge_base_url: str | None = None

    @property
    def home_kind(self) -> str:
        """Legacy field: default | kimi (any non-default profile)."""
        return HOME_DEFAULT if self.selection == HOME_DEFAULT else HOME_KIMI

    @home_kind.setter
    def home_kind(self, value: str) -> None:
        if value == HOME_DEFAULT:
            self.selection = HOME_DEFAULT
        elif value == HOME_KIMI:
            self.selection = self.profiles.active_id or "kimi"
        else:
            self.selection = value

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def home_path(self) -> Path:
        if self.selection == HOME_DEFAULT:
            return default_home_path()
        profile = self.profiles.get(self.selection) or self.profiles.get(self.profiles.active_id)
        if profile is not None:
            return self.profiles.home_for(profile.id)
        return kimi_home_path()

    def _capture_viewed_to_slot(self) -> None:
        if not self.viewed_id:
            return
        slot = self.slots.get(self.viewed_id)
        if slot is None:
            return
        slot.workspace = self.workspace
        slot.thread_id = self.thread_id
        slot.turn_id = self.turn_id
        slot.busy = self.busy

    def _apply_slot_to_viewed(self, slot: CodexSlot) -> None:
        self.viewed_id = slot.id
        self.workspace = slot.workspace
        self.thread_id = slot.thread_id
        self.turn_id = slot.turn_id
        self.busy = slot.busy

    def _viewed_slot(self) -> CodexSlot | None:
        if not self.viewed_id:
            return None
        return self.slots.get(self.viewed_id)

    def _bind_thread(self, slot_id: str, thread_id: str | None) -> None:
        if not thread_id:
            return
        # Drop stale reverse indexes for this slot.
        for tid, sid in list(self._thread_index.items()):
            if sid == slot_id and tid != thread_id:
                self._thread_index.pop(tid, None)
        self._thread_index[thread_id] = slot_id
        slot = self.slots.get(slot_id)
        if slot is not None:
            slot.thread_id = thread_id
        if slot_id == self.viewed_id:
            self.thread_id = thread_id
        try:
            self.store.touch(slot_id, native_id=thread_id)
        except Exception:  # noqa: BLE001
            pass

    def _clear_thread(self, slot: CodexSlot) -> None:
        """Forget a dead Codex thread id (process restart / resume miss)."""
        old = slot.thread_id
        if old:
            self._thread_index.pop(old, None)
        slot.thread_id = None
        if slot.id == self.viewed_id:
            self.thread_id = None
        try:
            self.store.touch(slot.id, native_id="")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _thread_id_from_result(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        thread = result.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
        if result.get("id") and result.get("object") == "thread":
            return str(result["id"])
        return ""

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

    @staticmethod
    def _extract_thread_id(params: dict[str, Any]) -> str:
        if params.get("threadId"):
            return str(params["threadId"])
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("threadId"):
            return str(turn["threadId"])
        item = params.get("item")
        if isinstance(item, dict) and item.get("threadId"):
            return str(item["threadId"])
        return ""

    def _slot_id_for_params(self, params: dict[str, Any]) -> str:
        tid = self._extract_thread_id(params)
        if tid and tid in self._thread_index:
            return self._thread_index[tid]
        busy = [slot for slot in self.slots.values() if slot.busy]
        if len(busy) == 1:
            return busy[0].id
        return self.viewed_id

    def _running_ids(self) -> list[str]:
        return [slot.id for slot in self.slots.values() if slot.busy]

    def _sessions_ui(self) -> dict[str, Any]:
        return self.store.ui_groups(
            viewed_id=self.viewed_id,
            viewed_workspace=str(self.workspace),
            running_ids=self._running_ids(),
            show_archived=self.show_archived,
        )

    def status_payload(self, *, include_transcript: bool = False) -> dict[str, Any]:
        profile = None
        if self.selection != HOME_DEFAULT:
            profile = self.profiles.get(self.selection)
        payload: dict[str, Any] = {
            "state": self.state,
            "alive": self.alive,
            "busy": self.busy,
            "home_kind": self.home_kind,
            "selection": self.selection,
            "profile_id": "" if self.selection == HOME_DEFAULT else self.selection,
            "home_path": str(self.home_path),
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "model": self.selected_model or self.model or (profile.model if profile else ""),
            "model_provider": self.model_provider or (profile.provider_id if profile else ""),
            "models": list(self.models),
            "selected_model": self.selected_model or self.model or "",
            "selected_effort": self.selected_effort,
            "effort_levels": self._effort_levels_for(self.selected_model or self.model or ""),
            "permission_mode": self.permission_mode,
            "workspace": str(self.workspace),
            "error": self.last_error,
            "executable": self._executable or find_codex_executable() or "",
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
        # Transcript is large — only attach on open/switch/reconnect, not every
        # turn/status tick (that was freezing the WebView under stream load).
        if include_transcript and self.viewed_id:
            payload["transcript"] = self.store.load_transcript(self.viewed_id)
        return payload

    async def push_status(self, *, include_transcript: bool = False) -> None:
        await self._push(
            "codex_status",
            self.status_payload(include_transcript=include_transcript),
        )

    def _ensure_viewed_meta(self) -> CodexSlot:
        """Create or hydrate the viewed panel session for the current workspace."""
        from .workspace import is_app_install_workspace, preferred_project_workspace

        if is_app_install_workspace(self.workspace):
            self.workspace = preferred_project_workspace(None)
            # Drop empty install-dir phantoms so they never reappear in the sidebar.
            for meta in list(self.store.list(include_archived=True)):
                if is_app_install_workspace(meta.workspace) and meta.message_count <= 0:
                    self.store.delete(meta.id)
        if self.viewed_id and self.viewed_id in self.slots:
            return self.slots[self.viewed_id]
        metas = self.store.list(workspace=self.workspace, include_archived=False)
        if metas:
            meta = metas[0]
        else:
            # Prefer any recent real project session over inventing one under cwd.
            any_metas = self.store.list(include_archived=False)
            if any_metas and not is_app_install_workspace(any_metas[0].workspace):
                meta = any_metas[0]
                self.workspace = Path(meta.workspace)
            else:
                meta = self.store.create(
                    self.workspace,
                    profile_id="" if self.selection == HOME_DEFAULT else self.selection,
                )
        slot = CodexSlot(
            id=meta.id,
            workspace=Path(meta.workspace) if meta.workspace else self.workspace,
            thread_id=meta.native_id or None,
        )
        self.slots[slot.id] = slot
        self._apply_slot_to_viewed(slot)
        if slot.thread_id:
            self._thread_index[slot.thread_id] = slot.id
        return slot

    async def _start_or_resume_thread(self, slot: CodexSlot) -> None:
        native = ""
        meta = self.store.get(slot.id)
        if meta and meta.native_id:
            native = meta.native_id
        elif slot.thread_id:
            native = slot.thread_id

        thread: Any = None
        resumed = False
        if native:
            try:
                thread = await self.request(
                    "thread/resume",
                    {"threadId": native, "cwd": str(slot.workspace)},
                    timeout=60.0,
                )
                if self._thread_id_from_result(thread):
                    resumed = True
                else:
                    raise CodexRuntimeError(f"thread resume returned no id ({native})")
            except CodexRuntimeError as error:
                # Dead id after app-server restart / orphan reap — start fresh.
                self._clear_thread(slot)
                await self._emit(
                    "codex_notice",
                    {
                        "level": "warn",
                        "text": (
                            f"Codex 会话线程已失效（{error}），"
                            "已开新线程续聊（界面历史仍在）"
                        ),
                    },
                    slot_id=slot.id,
                )
                thread = None

        if not resumed:
            thread = await self.request(
                "thread/start",
                {"cwd": str(slot.workspace)},
                timeout=60.0,
            )

        if isinstance(thread, dict) and slot.id == self.viewed_id:
            self.model = str(thread.get("model") or self.model)
            self.model_provider = str(thread.get("modelProvider") or self.model_provider)
        tid = self._thread_id_from_result(thread)
        if not tid:
            raise CodexRuntimeError("Codex 未返回 thread id")
        self._bind_thread(slot.id, tid)
        if self.last_error and "thread not found" in self.last_error.lower():
            self.last_error = ""
        if slot.id == self.viewed_id and not self.selected_model and self.model:
            self.selected_model = self.model

    async def _reap_orphan_clis(self) -> None:
        keep: set[int] = set()
        if self._process is not None and self._process.returncode is None and self._process.pid:
            keep.add(int(self._process.pid))
        try:
            killed = await asyncio.to_thread(
                reap_orphans,
                keep_pids=keep,
                kinds=(KIND_CODEX,),
            )
        except Exception:  # noqa: BLE001
            return
        tip = summarize_reaped(killed)
        if tip:
            await self._emit("codex_notice", {"level": "info", "text": tip})

    async def start(self) -> None:
        if self.alive and self.state == "ready":
            await self.push_status()
            return
        await self._reap_orphan_clis()
        await self.stop()
        self.state = "starting"
        self.last_error = ""
        await self.push_status()

        executable = self._executable or find_codex_executable()
        if not executable:
            self.state = "error"
            self.last_error = "未找到 codex 可执行文件。请安装 Codex CLI 并确保在 PATH 中。"
            await self._emit("codex_error", {"message": self.last_error})
            await self.push_status()
            return

        # Fix outdated generated homes before launch (wire_api=chat → responses).
        try:
            fixed = self.profiles.repair_homes()
            if fixed:
                await self._emit(
                    "codex_notice",
                    {"level": "info", "text": f"已自动修复 {fixed} 个旧 Profile 配置（wire_api→responses）"},
                )
        except Exception:  # noqa: BLE001
            pass

        bridge_notice = False
        try:
            bridge_notice = await self._prepare_bridge()
            env = self._build_env()
        except ValueError as error:
            self.state = "error"
            self.last_error = str(error)
            await self._emit("codex_error", {"message": self.last_error})
            await self.push_status()
            return
        except Exception as error:  # noqa: BLE001
            self.state = "error"
            self.last_error = f"Responses 桥接启动失败：{error}"
            await self._emit("codex_error", {"message": self.last_error})
            await self.push_status()
            return

        slot = self._ensure_viewed_meta()
        try:
            self._process = await asyncio.create_subprocess_exec(
                executable,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(slot.workspace),
                limit=MAX_LINE_BYTES,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, ValueError) as error:
            self.state = "error"
            self.last_error = f"无法启动 Codex：{error}"
            await self._stop_bridge()
            await self._emit("codex_error", {"message": self.last_error})
            await self.push_status()
            return

        if self._process and self._process.pid:
            register_child(
                int(self._process.pid),
                KIND_CODEX,
                command=f"{executable} app-server --listen stdio://",
            )
        self._executable = executable
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "aiharness-gui",
                        "title": "AIHarness Codex Panel",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=30.0,
            )
            await self.notify("initialized", {})
            await self._start_or_resume_thread(slot)
        except CodexRuntimeError as error:
            self.state = "error"
            self.last_error = str(error)
            await self._emit("codex_error", {"message": self.last_error})
            await self.stop()
            await self.push_status()
            return

        await self.refresh_models()
        self.state = "ready"
        label = self.selection if self.selection == HOME_DEFAULT else (
            (self.profiles.get(self.selection).name if self.profiles.get(self.selection) else self.selection)
        )
        profile = None if self.selection == HOME_DEFAULT else self.profiles.get(self.selection)
        route = proxy_mod.describe_setting(profile.proxy if profile else "")
        await self._emit(
            "codex_notice",
            {
                "level": "info",
                "text": f"Codex 已连接（{label} · {route} · {self.home_path}）",
            },
        )
        if bridge_notice:
            await self._emit(
                "codex_notice",
                {
                    "level": "info",
                    "text": "已启用本地 Responses↔Chat 桥接（Kimi Coding）",
                },
            )
        await self.push_status(include_transcript=True)

    async def _prepare_bridge(self) -> bool:
        """Start or stop the local Responses bridge for chat-only providers."""
        await self._stop_bridge()
        if self.selection == HOME_DEFAULT:
            return False
        profile = self.profiles.get(self.selection)
        if profile is None:
            self.profiles.ensure_defaults()
            profile = self.profiles.get(self.profiles.active_id) or self.profiles.get("kimi")
        if profile is None or not needs_responses_bridge(profile.base_url):
            return False
        accounts = self._agent_accounts() if self._agent_accounts else None
        key = self.profiles.resolve_api_key(profile, agent_accounts=accounts)
        bridge = ResponsesBridge()
        local = await bridge.start(profile.base_url, key, proxy=profile.proxy or "")
        self._responses_bridge = bridge
        self._bridge_base_url = local
        return True

    async def _stop_bridge(self) -> None:
        if self._responses_bridge is not None:
            try:
                await self._responses_bridge.stop()
            except Exception:  # noqa: BLE001
                pass
        self._responses_bridge = None
        self._bridge_base_url = None

    def _build_env(self) -> dict[str, str]:
        if self.selection == HOME_DEFAULT:
            home = default_home_path()
            home.mkdir(parents=True, exist_ok=True)
            return {**os.environ, "CODEX_HOME": str(home)}
        profile = self.profiles.get(self.selection)
        if profile is None:
            # Legacy "kimi" selection → active profile or seed.
            self.profiles.ensure_defaults()
            profile = self.profiles.get(self.profiles.active_id) or self.profiles.get("kimi")
        if profile is None:
            raise ValueError("没有可用的 Codex Profile，请先添加一个")
        self.selection = profile.id
        self.profiles.active_id = profile.id
        self.profiles.save()
        accounts = self._agent_accounts() if self._agent_accounts else None
        env = self.profiles.launch_env(
            profile,
            agent_accounts=accounts,
            base_url_override=self._bridge_base_url,
        )
        if not self.profiles.resolve_api_key(profile, agent_accounts=accounts):
            # Still allow start — user may have set the env var globally under another name.
            pass
        return env

    async def stop(self) -> None:
        self._stopping = True
        self._capture_viewed_to_slot()
        for slot in self.slots.values():
            slot.busy = False
            slot.turn_id = None
        self.busy = False
        self.turn_id = None
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CodexRuntimeError("Codex stopped"))
        self._pending.clear()

        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._process is not None:
            dead_pid = int(self._process.pid or 0)
            if self._process.returncode is None:
                try:
                    if self._process.stdin is not None:
                        self._process.stdin.close()
                    await asyncio.wait_for(self._process.wait(), timeout=SHUTDOWN_GRACE)
                except (asyncio.TimeoutError, ProcessLookupError, ValueError):
                    try:
                        self._process.kill()
                        await self._process.wait()
                    except (ProcessLookupError, ValueError):
                        pass
            unregister_child(dead_pid)
            self._process = None

        await self._stop_bridge()

        # Intentional shutdown is never an error state.
        self.state = "stopped"
        self._stopping = False
        await self.push_status()

    async def set_home(self, kind: str) -> None:
        """Back-compat: ``default`` / ``kimi`` / profile id."""
        await self.set_selection(kind)

    async def set_selection(self, selection: str) -> None:
        next_sel = (selection or "").strip() or HOME_KIMI
        if next_sel == HOME_KIMI:
            next_sel = self.profiles.active_id or "kimi"
        if next_sel == self.selection and self.alive and self.state == "ready":
            await self.push_status()
            return
        if next_sel != HOME_DEFAULT and self.profiles.get(next_sel) is None and next_sel != "kimi":
            raise ValueError(f"unknown Codex profile '{next_sel}'")
        self.selection = next_sel
        self.selected_model = ""
        self.selected_effort = ""
        self.models = []
        self.model = ""
        self.model_provider = ""
        if next_sel != HOME_DEFAULT and self.profiles.get(next_sel):
            self.profiles.set_active(next_sel)
            profile = self.profiles.get(next_sel)
            if profile and profile.model:
                self.selected_model = profile.model
                self.model = profile.model
        await self.stop()
        await self.start()

    async def upsert_profile(self, args: dict[str, Any]) -> None:
        profile = self.profiles.upsert(
            profile_id=str(args.get("id") or ""),
            name=str(args.get("name") or ""),
            base_url=str(args.get("base_url") or ""),
            model=str(args.get("model") or ""),
            env_key=str(args.get("env_key") or ""),
            wire_api=str(args.get("wire_api") or "responses"),
            provider_id=str(args.get("provider_id") or ""),
            template=str(args.get("template") or "custom"),
            api_key=str(args.get("api_key") or ""),
            proxy=str(args.get("proxy") or ""),
            note=str(args.get("note") or ""),
            make_active=bool(args.get("make_active")),
        )
        await self._emit(
            "codex_notice",
            {"level": "info", "text": f"已保存 Profile：{profile.name}"},
        )
        if args.get("activate") or args.get("make_active"):
            await self.set_selection(profile.id)
        else:
            await self.push_status()

    async def delete_profile(self, profile_id: str) -> None:
        if not self.profiles.delete(profile_id):
            raise ValueError(f"unknown Codex profile '{profile_id}'")
        if self.selection == profile_id:
            await self.set_selection(self.profiles.active_id or HOME_DEFAULT)
        else:
            await self.push_status()

    async def import_account(self, account_id: str, *, activate: bool = True) -> None:
        accounts = self._agent_accounts() if self._agent_accounts else []
        account = next((a for a in accounts if a.id == account_id), None)
        if account is None:
            raise ValueError(f"unknown Agent account '{account_id}'")
        profile = self.profiles.import_from_account(account, make_active=activate)
        await self._emit(
            "codex_notice",
            {"level": "info", "text": f"已从 Agent 导入 Profile：{profile.name}"},
        )
        if activate:
            await self.set_selection(profile.id)
        else:
            await self.push_status()

    async def new_session(self, workspace: Path | None = None) -> None:
        self._capture_viewed_to_slot()
        ws = Path(workspace) if workspace is not None else self.workspace
        meta = self.store.create(
            ws,
            profile_id="" if self.selection == HOME_DEFAULT else self.selection,
        )
        slot = CodexSlot(id=meta.id, workspace=ws)
        self.slots[slot.id] = slot
        self._apply_slot_to_viewed(slot)
        if self.alive and self.state == "ready":
            try:
                await self._start_or_resume_thread(slot)
            except CodexRuntimeError as error:
                self.last_error = str(error)
                await self._emit("codex_error", {"message": self.last_error}, slot_id=slot.id)
        await self.push_status(include_transcript=True)

    async def open_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        if sid == self.viewed_id:
            await self.push_status(include_transcript=True)
            return
        meta = self.store.get(sid)
        if meta is None:
            await self._emit("codex_error", {"message": "没有这个 Codex 会话"})
            return
        self._capture_viewed_to_slot()
        slot = self.slots.get(sid)
        if slot is None:
            slot = CodexSlot(
                id=meta.id,
                workspace=Path(meta.workspace) if meta.workspace else self.workspace,
                thread_id=meta.native_id or None,
            )
            self.slots[sid] = slot
        self._apply_slot_to_viewed(slot)
        # Always re-bind against the live app-server. Indexing a saved native_id
        # without resume left turn/start failing with "thread not found".
        if self.alive and self.state == "ready":
            try:
                await self._start_or_resume_thread(slot)
            except CodexRuntimeError as error:
                self.last_error = str(error)
                await self._emit("codex_error", {"message": self.last_error}, slot_id=slot.id)
        await self.push_status(include_transcript=True)

    async def delete_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        slot = self.slots.pop(sid, None)
        if slot and slot.thread_id:
            self._thread_index.pop(slot.thread_id, None)
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
            await self._emit("codex_error", {"message": "没有这个 Codex 会话"})
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
        """Change workspace for the viewed slot only; start a new thread for it."""
        self._capture_viewed_to_slot()
        slot = self._viewed_slot()
        if slot is None:
            slot = self._ensure_viewed_meta()
        slot.workspace = workspace
        self.workspace = workspace
        slot.busy = False
        slot.turn_id = None
        self.busy = False
        self.turn_id = None
        if slot.thread_id:
            self._thread_index.pop(slot.thread_id, None)
            slot.thread_id = None
            self.thread_id = None
        self.store.touch(slot.id, workspace=workspace, native_id="")
        if self.alive and self.state == "ready":
            try:
                await self._start_or_resume_thread(slot)
            except CodexRuntimeError as error:
                self.last_error = str(error)
                await self._emit("codex_error", {"message": self.last_error}, slot_id=slot.id)
                return
        await self.push_status()

    async def set_workspace(self, workspace: Path) -> None:
        """Panel-owned workspace change (viewed slot only)."""
        await self.set_panel_workspace(workspace)

    async def forget_workspace(self, workspace: Path | str) -> None:
        removed = self.store.delete_workspace(workspace)
        want = str(workspace)
        for sid, slot in list(self.slots.items()):
            if str(slot.workspace) == want:
                if slot.thread_id:
                    self._thread_index.pop(slot.thread_id, None)
                self.slots.pop(sid, None)
        if str(self.workspace) == want or (self.viewed_id and self.viewed_id not in self.slots):
            self.viewed_id = ""
            remaining = self.store.list(include_archived=False)
            if remaining:
                await self.open_session(remaining[0].id)
            else:
                await self.new_session(self.workspace if Path(self.workspace).is_dir() else Path.cwd())
            return
        if removed:
            await self.push_status()

    async def set_show_archived(self, show: bool) -> None:
        self.show_archived = bool(show)
        await self.push_status()

    async def prompt(self, text: str, images: list[dict[str, Any]] | None = None) -> None:
        text = (text or "").strip()
        images = list(images or [])
        if not text and not images:
            return
        if not self.alive or self.state != "ready":
            await self.start()
        slot = self._viewed_slot() or self._ensure_viewed_meta()
        if not self.alive or self.state != "ready" or not slot.thread_id:
            await self._emit(
                "codex_error",
                {"message": self.last_error or "Codex 未就绪"},
                slot_id=slot.id,
            )
            return
        if slot.busy:
            await self._emit(
                "codex_notice",
                {"level": "warn", "text": "Codex 正在处理上一轮，请稍候或中断"},
                slot_id=slot.id,
            )
            return

        slot.busy = True
        self.busy = True
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
        await self._emit("codex_activity", {"text": "Codex 正在处理…", "kind": "busy"}, slot_id=slot.id)
        await self.push_status()
        inputs: list[dict[str, Any]] = []
        if text:
            inputs.append({"type": "text", "text": text})
        for image in images:
            item = _codex_image_input(image)
            if item:
                inputs.append(item)
        try:
            turn_params: dict[str, Any] = {
                "threadId": slot.thread_id,
                "cwd": str(slot.workspace),
                "input": inputs,
            }
            if self.selected_model:
                turn_params["model"] = self.selected_model
            if self.selected_effort:
                turn_params["effort"] = self.selected_effort
            try:
                result = await self.request(
                    "turn/start",
                    turn_params,
                    timeout=REQUEST_TIMEOUT,
                )
            except CodexRuntimeError as error:
                # Recover once when the saved thread died with the previous process.
                if "thread not found" not in str(error).lower():
                    raise
                self._clear_thread(slot)
                await self._start_or_resume_thread(slot)
                turn_params["threadId"] = slot.thread_id
                result = await self.request(
                    "turn/start",
                    turn_params,
                    timeout=REQUEST_TIMEOUT,
                )
                await self._emit(
                    "codex_notice",
                    {
                        "level": "warn",
                        "text": "Codex 线程已重建，本轮已自动重试",
                    },
                    slot_id=slot.id,
                )
            turn = result.get("turn") if isinstance(result, dict) else None
            if isinstance(turn, dict) and turn.get("id"):
                slot.turn_id = str(turn["id"])
                self.turn_id = slot.turn_id
            await self.push_status()
        except CodexRuntimeError as error:
            slot.busy = False
            self.busy = False
            self.last_error = str(error)
            await self._emit("codex_error", {"message": self.last_error}, slot_id=slot.id)
            await self._emit("codex_done", {"ok": False}, slot_id=slot.id)
            await self.push_status()

    async def interrupt(self) -> None:
        slot = self._viewed_slot()
        if not self.alive or slot is None or not slot.thread_id or not slot.turn_id:
            return
        try:
            await self.request(
                "turn/interrupt",
                {"threadId": slot.thread_id, "turnId": slot.turn_id},
                timeout=30.0,
            )
        except CodexRuntimeError as error:
            await self._emit("codex_notice", {"level": "warn", "text": str(error)}, slot_id=slot.id)

    async def refresh_models(self) -> None:
        """Build the model picker: provider catalog for profiles, Codex list for default."""
        if not self.alive:
            return
        if self.selection != HOME_DEFAULT:
            profile = self.profiles.get(self.selection)
            if profile is not None:
                models = await self._fetch_provider_models(profile)
                if not models:
                    models = list(KNOWN_PROVIDER_MODELS.get(profile.template, []))
                # Always keep the profile's configured model visible.
                if profile.model and profile.model not in {m["id"] for m in models}:
                    models.insert(
                        0,
                        {
                            "id": profile.model,
                            "label": profile.model,
                            "efforts": self._default_efforts_for(profile.template, profile.model),
                            "default_effort": "",
                        },
                    )
                self.models = models
                if not self.selected_model:
                    self.selected_model = profile.model or (models[0]["id"] if models else "")
                self._sync_effort_for_model(self.selected_model)
                return

        # System default ~/.codex — use native Codex catalog (includes effort metadata).
        try:
            result = await self.request(
                "model/list",
                {"includeHidden": False},
                timeout=30.0,
            )
        except CodexRuntimeError as error:
            await self._emit("codex_notice", {"level": "warn", "text": f"模型列表：{error}"})
            return
        data = result.get("data") if isinstance(result, dict) else None
        models: list[dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                parsed = _parse_codex_model_entry(item)
                if parsed:
                    models.append(parsed)
        self.models = models
        if not self.selected_model and models:
            default = next((m for m in models if m.get("is_default")), None)
            self.selected_model = str((default or models[0])["id"])
        self._sync_effort_for_model(self.selected_model)

    async def _fetch_provider_models(self, profile: Any) -> list[dict[str, Any]]:
        """GET OpenAI-compatible ``/models`` for the active profile's endpoint."""
        base = (profile.base_url or "").rstrip("/")
        if not base:
            return []
        url = f"{base}/models"
        accounts = self._agent_accounts() if self._agent_accounts else None
        key = self.profiles.resolve_api_key(profile, agent_accounts=accounts)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        client_kwargs: dict[str, Any] = {"timeout": 12.0}
        # Mirror the profile's network egress for the catalog fetch.
        try:
            setting = proxy_mod.normalise(profile.proxy or "")
        except proxy_mod.ProxyError:
            setting = ""
        if setting == proxy_mod.DIRECT:
            client_kwargs["trust_env"] = False
        elif setting:
            client_kwargs["trust_env"] = False
            client_kwargs["proxy"] = setting
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as error:  # noqa: BLE001
            await self._emit(
                "codex_notice",
                {
                    "level": "warn",
                    "text": f"未能从供应商拉取模型列表（{error}），已用内置目录",
                },
            )
            return []
        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        known = {
            m["id"]: m for m in KNOWN_PROVIDER_MODELS.get(profile.template, []) if m.get("id")
        }
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or item.get("model") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            hint = known.get(mid, {})
            models.append(
                {
                    "id": mid,
                    "label": mid,
                    "efforts": list(
                        hint.get("efforts")
                        or self._default_efforts_for(profile.template, mid)
                    ),
                    "default_effort": str(hint.get("default_effort") or ""),
                }
            )
        # Prefer known ordering (k3 first) when present.
        if known:
            order = {mid: idx for idx, mid in enumerate(known)}
            models.sort(key=lambda m: order.get(m["id"], 1000))
        return models

    def _effort_levels_for(self, model_id: str) -> list[str]:
        for item in self.models:
            if item.get("id") == model_id:
                levels = item.get("efforts") or []
                return [str(level) for level in levels if level]
        profile = None if self.selection == HOME_DEFAULT else self.profiles.get(self.selection)
        template = profile.template if profile else ""
        return self._default_efforts_for(template, model_id)

    def _default_efforts_for(self, template: str, model_id: str) -> list[str]:
        for item in KNOWN_PROVIDER_MODELS.get(template, []):
            if item.get("id") == model_id:
                return list(item.get("efforts") or [])
        mid = (model_id or "").lower()
        if mid.startswith("k3") or mid.startswith("kimi-k3") or mid.startswith("kimi-for-coding"):
            return ["low", "high", "max"]
        if mid.startswith("kimi-") or mid.startswith("gpt-"):
            return ["low", "medium", "high"]
        return []

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
        # Persist onto active profile when not using system default home.
        if self.selection != HOME_DEFAULT:
            profile = self.profiles.get(self.selection)
            if profile is not None:
                profile.model = model_id
                self.profiles.save()
                self.profiles.materialize(
                    profile,
                    base_url_override=self._bridge_base_url,
                )
        self.model = model_id
        self._sync_effort_for_model(model_id)
        await self.push_status()
        await self._emit(
            "codex_notice",
            {"level": "info", "text": f"已选择模型：{model_id}（下一轮生效）"},
        )

    async def set_effort(self, effort: str) -> None:
        effort = (effort or "").strip()
        levels = self._effort_levels_for(self.selected_model or self.model or "")
        if effort and levels and effort not in levels:
            await self._emit(
                "codex_notice",
                {"level": "warn", "text": f"当前模型不支持 effort={effort}"},
            )
            return
        self.selected_effort = effort
        await self.push_status()
        await self._emit(
            "codex_notice",
            {
                "level": "info",
                "text": f"已选择 effort：{effort or '默认'}（下一轮生效）",
            },
        )

    async def set_permission_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode not in {"ask", "auto", "yolo"}:
            await self._emit(
                "codex_notice",
                {"level": "warn", "text": f"未知权限模式：{mode}"},
            )
            return
        self.permission_mode = mode
        await self.push_status()
        await self._emit(
            "codex_notice",
            {"level": "info", "text": f"权限模式：{mode}"},
        )

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT
    ) -> Any:
        if not self.alive or self._process is None or self._process.stdin is None:
            raise CodexRuntimeError("Codex app-server is not running")
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        try:
            async with self._write_lock:
                await self._write(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as error:
            self._pending.pop(req_id, None)
            raise CodexRuntimeError(f"timed out waiting for {method}") from error
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.alive:
            return
        async with self._write_lock:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                }
            )

    async def _write(self, payload: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001
            self.last_error = f"Codex 读取失败：{error}"
            await self._emit("codex_error", {"message": self.last_error})
        finally:
            if not self._stopping and self.state in {"ready", "starting"}:
                self.state = "error"
                self.last_error = self.last_error or "Codex app-server 已退出"
                await self._emit("codex_error", {"message": self.last_error})
            for slot in self.slots.values():
                slot.busy = False
            self.busy = False
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(CodexRuntimeError("Codex connection closed"))
            self._pending.clear()
            if not self._stopping:
                await self.push_status()

    async def _dispatch_message(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method")

        if method and msg_id is not None and "result" not in message and "error" not in message:
            # Server → client request (approvals, etc.)
            asyncio.create_task(self._handle_server_request(msg_id, str(method), message.get("params") or {}))
            return

        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                if "error" in message:
                    err = message.get("error") or {}
                    detail = err.get("message") if isinstance(err, dict) else str(err)
                    future.set_exception(CodexRuntimeError(detail or "Codex RPC error"))
                else:
                    future.set_result(message.get("result"))
            return

        if method:
            await self._handle_notification(str(method), message.get("params") or {})

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if not isinstance(params, dict):
            params = {}
        slot_id = self._slot_id_for_params(params)
        slot = self.slots.get(slot_id) if slot_id else None

        def _mirror_busy(busy: bool, turn_id: str | None = None) -> None:
            if slot is not None:
                slot.busy = busy
                if turn_id is not None or not busy:
                    slot.turn_id = turn_id
            if slot_id == self.viewed_id or (not slot_id and slot is None):
                self.busy = busy
                if turn_id is not None or not busy:
                    self.turn_id = turn_id

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if delta:
                if slot is not None:
                    slot.streamed_text = True
                    if not slot.activity_streaming:
                        slot.activity_streaming = True
                        await self._emit(
                            "codex_activity",
                            {"text": "回答中…", "kind": "streaming"},
                            slot_id=slot_id,
                        )
                await self._emit("codex_text", {"delta": str(delta)}, slot_id=slot_id)
            return
        if method in {
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
        }:
            delta = params.get("delta")
            if delta:
                if slot is not None and not slot.activity_thinking:
                    slot.activity_thinking = True
                    await self._emit(
                        "codex_activity",
                        {"text": "思考中…", "kind": "thinking"},
                        slot_id=slot_id,
                    )
                await self._emit("codex_thinking", {"delta": str(delta)}, slot_id=slot_id)
            return
        if method == "item/reasoning/summaryPartAdded":
            if slot is not None and not slot.activity_thinking:
                slot.activity_thinking = True
                await self._emit(
                    "codex_activity",
                    {"text": "思考中…", "kind": "thinking"},
                    slot_id=slot_id,
                )
            return
        if method == "item/commandExecution/outputDelta":
            # High-frequency; final aggregated output arrives on item/completed.
            return
        if method == "item/started":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            await self._emit_item_started(item, slot_id=slot_id)
            return
        if method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            await self._emit_item_completed(item, slot_id=slot_id)
            return
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
            turn_id = str(turn["id"]) if isinstance(turn, dict) and turn.get("id") else None
            if slot is not None:
                slot.streamed_text = False
                slot.activity_streaming = False
                slot.activity_thinking = False
            _mirror_busy(True, turn_id)
            await self._emit("codex_activity", {"text": "Codex 正在处理…", "kind": "busy"}, slot_id=slot_id)
            await self.push_status()
            return
        if method == "turn/completed":
            if slot is not None:
                slot.streamed_text = False
                slot.activity_streaming = False
                slot.activity_thinking = False
            _mirror_busy(False, None)
            # Best-effort: persist assistant buffer is handled by stream; mark done.
            await self._emit("codex_activity", {"text": "", "kind": "clear"}, slot_id=slot_id)
            # Bridge failures can still yield an empty turn/completed from Codex.
            bridge_err = ""
            if self._responses_bridge is not None:
                bridge_err = (self._responses_bridge.last_error or "").strip()
                if bridge_err:
                    self._responses_bridge.last_error = ""
                    self.last_error = bridge_err
            if bridge_err:
                tip = _codex_error_text({"message": bridge_err})
                await self._emit(
                    "codex_error",
                    {"message": f"模型接口错误：{tip}"},
                    slot_id=slot_id,
                )
                await self._emit("codex_done", {"ok": False}, slot_id=slot_id)
            else:
                await self._emit("codex_done", {"ok": True}, slot_id=slot_id)
            await self.push_status()
            return
        if method in {"error", "turn/error"} or method.endswith("/error"):
            text = _codex_error_text(params)
            will_retry = params.get("willRetry")
            if will_retry:
                await self._emit(
                    "codex_notice",
                    {"level": "warn", "text": f"重试中：{text}"},
                    slot_id=slot_id,
                )
            else:
                _mirror_busy(False, None)
                self.last_error = text
                await self._emit("codex_notice", {"level": "error", "text": text}, slot_id=slot_id)
                await self._emit("codex_done", {"ok": False}, slot_id=slot_id)
                await self.push_status()
            return

    async def _emit_item_started(self, item: dict[str, Any], *, slot_id: str = "") -> None:
        call_id, name, headline, args = _codex_item_view(item)
        if not call_id:
            return
        kind = str(item.get("type") or "")
        # Chat messages are not tools — opening a tool card here leaves a forever spinner
        # when item/completed intentionally skips tool_end.
        if kind in {"agentMessage", "reasoning", "plan", "userMessage"}:
            await self._emit(
                "codex_activity",
                {
                    "text": (
                        "已收到消息…"
                        if kind == "userMessage"
                        else ("思考中…" if kind == "reasoning" else "回答中…")
                    ),
                    "kind": "thinking" if kind == "reasoning" else "busy",
                },
                slot_id=slot_id,
            )
            return
        await self._emit("codex_activity", {"text": headline, "kind": "busy"}, slot_id=slot_id)
        await self._emit(
            "codex_tool_start",
            {
                "call_id": call_id,
                "name": name,
                "headline": headline,
                "args": args,
            },
            slot_id=slot_id,
        )

    async def _emit_item_completed(self, item: dict[str, Any], *, slot_id: str = "") -> None:
        call_id, name, headline, args = _codex_item_view(item)
        if not call_id:
            return
        kind = str(item.get("type") or "")
        if kind in {"agentMessage", "reasoning", "plan"}:
            if kind == "agentMessage":
                text = _agent_message_text(item)
                slot = self.slots.get(slot_id) if slot_id else None
                already = bool(slot and slot.streamed_text)
                if text and not already:
                    # Some providers (Kimi via bridge) deliver the full message only
                    # on item/completed — without deltas the UI would stay blank.
                    await self._emit("codex_text", {"delta": text}, slot_id=slot_id)
                if text and slot_id:
                    try:
                        self.store.append_transcript(
                            slot_id,
                            PanelTranscriptEntry(role="assistant", text=text),
                        )
                    except Exception:  # noqa: BLE001
                        pass
            elif kind == "reasoning":
                summary = item.get("summary") if isinstance(item.get("summary"), list) else []
                content = item.get("content") if isinstance(item.get("content"), list) else []
                bits = [str(part) for part in (summary or content) if part]
                if bits:
                    await self._emit(
                        "codex_thinking",
                        {"delta": "\n".join(bits)},
                        slot_id=slot_id,
                    )
            return
        # userMessage completion is acknowledgement only — show a light check, not a tool card.
        if kind == "userMessage":
            await self._emit(
                "codex_activity",
                {"text": "已收到消息…", "kind": "busy"},
                slot_id=slot_id,
            )
            return
        status = str(item.get("status") or "").lower()
        is_error = status in {"failed", "declined", "error"}
        if item.get("exitCode") not in (None, 0, "0"):
            try:
                if int(item.get("exitCode")) != 0:
                    is_error = True
            except (TypeError, ValueError):
                pass
        duration = 0.0
        if item.get("durationMs") is not None:
            try:
                duration = float(item["durationMs"]) / 1000.0
            except (TypeError, ValueError):
                duration = 0.0
        content = ""
        if item.get("aggregatedOutput"):
            content = str(item.get("aggregatedOutput"))
        elif item.get("result") is not None:
            content = json.dumps(item.get("result"), ensure_ascii=False)[:4000]
        elif item.get("error"):
            content = str(item.get("error"))
            is_error = True
        summary = headline
        if kind == "commandExecution" and item.get("exitCode") is not None:
            summary = f"{headline} → exit {item.get('exitCode')}"
        await self._emit(
            "codex_tool_end",
            {
                "call_id": call_id,
                "name": name,
                "summary": summary,
                "content": content[:8000],
                "is_error": is_error,
                "duration": duration,
                "args": args,
            },
            slot_id=slot_id,
        )

    async def _handle_server_request(self, msg_id: Any, method: str, params: dict[str, Any]) -> None:
        if not isinstance(params, dict):
            params = {}
        slot_id = self._slot_id_for_params(params)
        result: dict[str, Any] = {"decision": "decline"}
        try:
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "execCommandApproval",
                "applyPatchApproval",
                "permissions/requestApproval",
            }:
                kind = "command" if "command" in method.lower() or "exec" in method.lower() else "file"
                if "permission" in method.lower():
                    kind = "permission"
                command = params.get("command")
                if isinstance(command, list):
                    command = " ".join(str(part) for part in command)
                detail = str(
                    command
                    or params.get("reason")
                    or params.get("summary")
                    or json.dumps(params, ensure_ascii=False)[:1200]
                )
                auto = _auto_approval_for_mode(self.permission_mode)
                if auto is not None:
                    answer = auto
                else:
                    answer = await self._park_approval(
                        {
                            "kind": kind,
                            "method": method,
                            "tool": "Codex",
                            "reason": f"Codex 原生请求确认（{method}）",
                            "detail": detail,
                            "params": params,
                            "panel_session_id": slot_id,
                        }
                    )
                result = {"decision": _map_approval_decision(answer)}
            elif method in {
                "item/tool/requestUserInput",
                "tool/requestUserInput",
            }:
                # Forward native questions; UI answers as free-text / accept.
                # Free-text prompts still need a human even in auto/yolo.
                detail = str(
                    params.get("question")
                    or params.get("prompt")
                    or json.dumps(params, ensure_ascii=False)[:1200]
                )
                answer = await self._park_approval(
                    {
                        "kind": "ask",
                        "method": method,
                        "tool": "Codex",
                        "reason": "Codex 询问",
                        "detail": detail,
                        "params": params,
                        "panel_session_id": slot_id,
                    }
                )
                # Prefer structured answers when present; otherwise wrap decision.
                if isinstance(answer, dict):
                    result = answer
                else:
                    result = {"answers": {"response": str(answer or "")}}
            else:
                # Still surface unknown native requests instead of silently eating them.
                detail = json.dumps(params, ensure_ascii=False)[:1200]
                auto = _auto_approval_for_mode(self.permission_mode)
                if auto is not None:
                    answer = auto
                else:
                    answer = await self._park_approval(
                        {
                            "kind": "other",
                            "method": method,
                            "tool": "Codex",
                            "reason": f"Codex 请求（{method}）",
                            "detail": detail,
                            "params": params,
                            "panel_session_id": slot_id,
                        }
                    )
                result = {"decision": _map_approval_decision(answer)}
        except Exception as error:  # noqa: BLE001
            await self._emit("codex_notice", {"level": "error", "text": str(error)}, slot_id=slot_id)
            result = {"decision": "decline"}

        try:
            async with self._write_lock:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": result,
                    }
                )
        except Exception as error:  # noqa: BLE001
            await self._emit(
                "codex_notice",
                {"level": "error", "text": f"审批回复失败：{error}"},
                slot_id=slot_id,
            )

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            async for raw in self._process.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-40]
        except (asyncio.CancelledError, ValueError):
            return


def _parse_codex_model_entry(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    mid = str(item.get("id") or item.get("model") or "").strip()
    if not mid or item.get("hidden"):
        return None
    efforts: list[str] = []
    raw_efforts = item.get("supportedReasoningEfforts") or item.get("supported_reasoning_efforts") or []
    if isinstance(raw_efforts, list):
        for entry in raw_efforts:
            if isinstance(entry, dict):
                value = str(entry.get("reasoningEffort") or entry.get("effort") or "").strip()
            else:
                value = str(entry or "").strip()
            if value and value not in efforts:
                efforts.append(value)
    default_effort = str(
        item.get("defaultReasoningEffort") or item.get("default_reasoning_effort") or ""
    ).strip()
    return {
        "id": mid,
        "label": str(item.get("displayName") or item.get("model") or mid),
        "efforts": efforts,
        "default_effort": default_effort,
        "is_default": bool(item.get("isDefault") or item.get("is_default")),
    }


def _codex_item_view(item: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    """Return ``(call_id, name, headline, args)`` for UI tool cards / activity."""
    if not isinstance(item, dict):
        return "", "", "", {}
    call_id = str(item.get("id") or "").strip()
    kind = str(item.get("type") or "").strip()
    args: dict[str, Any] = {}
    if kind == "commandExecution":
        command = str(item.get("command") or "").strip()
        args = {"command": command, "cwd": item.get("cwd") or ""}
        short = command if len(command) <= 90 else command[:87] + "…"
        return call_id, "command", (f"$ {short}" if short else "运行命令"), args
    if kind == "fileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        paths: list[str] = []
        for change in changes[:4]:
            if isinstance(change, dict):
                path = str(change.get("path") or change.get("file") or "").strip()
                if path:
                    paths.append(path)
        args = {"paths": paths, "changes": changes[:8]}
        label = ", ".join(Path(p).name for p in paths) if paths else "文件改动"
        return call_id, "fileChange", f"编辑 {label}", args
    if kind == "mcpToolCall":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or "")
        args = {"server": server, "tool": tool, "arguments": item.get("arguments")}
        return call_id, "mcp", f"MCP {server}.{tool}".strip("."), args
    if kind == "dynamicToolCall":
        tool = str(item.get("tool") or "tool")
        args = {"tool": tool, "arguments": item.get("arguments")}
        return call_id, tool, f"工具 {tool}", args
    if kind == "reasoning":
        return call_id, "reasoning", "思考中…", {}
    if kind == "agentMessage":
        return call_id, "message", "回答中…", {}
    if kind == "plan":
        return call_id, "plan", "更新计划…", {}
    if kind:
        return call_id, kind, kind, dict(item)
    return call_id, "item", "Codex 步骤", {}


def _auto_approval_for_mode(mode: str) -> str | None:
    """Return a synthetic HITL answer when mode skips the park dialog."""
    if mode == "yolo":
        return "acceptForSession"
    if mode == "auto":
        return "accept"
    return None


def _codex_error_text(params: dict[str, Any]) -> str:
    """Flatten nested Codex / bridge error blobs into one readable line."""
    raw = params.get("message") or params.get("error") or params
    text = ""
    if isinstance(raw, dict):
        text = str(raw.get("message") or raw)
    else:
        text = str(raw)
    # Codex often wraps bridge JSON as a stringified object.
    for _ in range(3):
        candidate = text.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                break
            if isinstance(parsed, dict):
                nested = parsed.get("error")
                if isinstance(nested, dict) and nested.get("message"):
                    text = str(nested["message"])
                    continue
                if parsed.get("message"):
                    text = str(parsed["message"])
                    continue
            break
        # Python-repr style: {'message': '{"error": ...}'}
        if "Request Entity Too Large" in text:
            text = (
                "请求体过大（会话上下文太长）。"
                "请新开一个 Codex 会话，或先断开再连接后开新对话继续。"
            )
            break
        break
    if "Request Entity Too Large" in text or "too large" in text.lower():
        return (
            "请求体过大（会话上下文太长）。"
            "请新开一个 Codex 会话继续；旧会话历史仍可在侧栏查看。"
        )
    return text[:500]


def _agent_message_text(item: dict[str, Any]) -> str:
    """Extract assistant text from a completed agentMessage item."""
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                piece = part.get("text") or part.get("content") or ""
                if piece:
                    parts.append(str(piece))
        return "".join(parts)
    return ""


def _map_approval_decision(answer: str | None) -> str:
    """Map GUI HITL answers onto Codex approval decisions."""
    if not answer:
        return "decline"
    value = str(answer).strip().lower()
    if value in {"accept", "once", "allow", "yes", "approve"}:
        return "accept"
    if value in {"acceptforsession", "accept_for_session", "always"}:
        return "acceptForSession"
    if value in {"cancel"}:
        return "cancel"
    return "decline"


def _codex_image_input(image: dict[str, Any]) -> dict[str, Any] | None:
    """Map UI attachment dicts onto Codex native UserInput image shapes."""
    path = str(image.get("path") or "").strip()
    if path:
        return {"type": "localImage", "path": path}
    url = str(image.get("data_url") or image.get("url") or "").strip()
    if url:
        return {"type": "image", "url": url}
    return None
