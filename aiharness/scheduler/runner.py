"""The background scheduler.

Ticks on a timer, runs whatever is due, and records the outcome. Jobs execute
in a fresh session with their own permission mode, so an unattended run can
never inherit approval state from the interactive conversation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from ..config.schema import Config
from ..constants import (
    SCHEDULER_CATCHUP_GRACE_SECONDS,
    SCHEDULER_MAX_CONCURRENT,
    SCHEDULER_TICK_SECONDS,
)
from ..permissions import PermissionEngine
from ..providers.router import NoRouteError, Router, Selection
from ..session.store import SessionStore
from ..skills import SkillLibrary
from ..toolset import build_registry
from .jobs import JobStore, RunRecord, ScheduledJob

#: Called with (job, record) after every run, so the UI can surface results.
RunListener = Callable[[ScheduledJob, RunRecord], None]
#: Alternative execution strategy, used by tests and by the CLI's dry run.
JobExecutor = Callable[[ScheduledJob], Awaitable[RunRecord]]


class Scheduler:
    """Runs scheduled jobs in the background of a live session."""

    def __init__(
        self,
        config: Config,
        router: Router,
        store: JobStore,
        *,
        sessions: SessionStore | None = None,
        on_run: RunListener | None = None,
        executor: JobExecutor | None = None,
    ):
        self.config = config
        self.router = router
        self.store = store
        self.sessions = sessions or SessionStore()
        self._on_run = on_run
        self._executor = executor or self.execute_job
        self._task: asyncio.Task | None = None
        self._semaphore = asyncio.Semaphore(SCHEDULER_MAX_CONCURRENT)
        self._running: set[str] = set()
        self._stop = asyncio.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Begin ticking. Safe to call twice."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="aih-scheduler")

    async def stop(self) -> None:
        """Stop ticking and wait for the loop to exit."""
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def running_jobs(self) -> set[str]:
        return set(self._running)

    # -- the tick ---------------------------------------------------------

    async def _loop(self) -> None:
        # Bring every job's next_run up to date before the first tick, so a
        # job whose window was missed while the app was closed is either run
        # (inside the grace period) or rescheduled rather than lost.
        self._reconcile()
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad job must not kill the loop
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SCHEDULER_TICK_SECONDS)
            except asyncio.TimeoutError:
                continue

    def _reconcile(self) -> None:
        """Fill in missing next_run values and reschedule overdue jobs."""
        now = time.time()
        changed = False
        for job in self.store.all():
            if not job.enabled:
                continue
            if not job.next_run:
                job.compute_next_run()
                changed = True
            elif job.is_overdue(now, SCHEDULER_CATCHUP_GRACE_SECONDS):
                job.compute_next_run()
                changed = True
        if changed:
            self.store.save()

    async def _tick(self) -> None:
        now = time.time()
        for job in self.store.all():
            if job.id in self._running:
                continue
            if not job.is_due(now, SCHEDULER_CATCHUP_GRACE_SECONDS):
                continue
            asyncio.create_task(self._run_guarded(job), name=f"aih-job-{job.id}")

    async def _run_guarded(self, job: ScheduledJob) -> None:
        self._running.add(job.id)
        try:
            async with self._semaphore:
                record = await self._executor(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            record = RunRecord(
                started_at=time.time(),
                finished_at=time.time(),
                ok=False,
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            self._running.discard(job.id)

        job.record_run(record)
        job.compute_next_run(datetime.now())
        self.store.update(job)
        if self._on_run:
            self._on_run(job, record)

    # -- execution --------------------------------------------------------

    async def execute_job(self, job: ScheduledJob) -> RunRecord:
        """Run one job to completion in its own session.

        Args:
          job: The job to execute.

        Returns:
          A :class:`RunRecord` describing what happened.
        """
        from ..agent.loop import Agent, Done, Notice

        record = RunRecord(started_at=time.time())
        workspace = Path(job.workspace).expanduser()
        if not workspace.is_dir():
            record.finished_at = time.time()
            record.error = f"workspace does not exist: {workspace}"
            return record

        try:
            selection = self._selection_for(job)
        except NoRouteError as error:
            record.finished_at = time.time()
            record.error = str(error)
            return record

        permissions = self._permissions_for(job, workspace)
        session = self.sessions.create(
            workspace, model=selection.model_id, account=selection.account_id or ""
        )
        session.rename(f"[scheduled] {job.name}")
        record.session_id = session.meta.id

        config = self._config_for(job)
        agent = Agent(
            config,
            self.router,
            build_registry(),
            permissions,
            workspace,
            skills=SkillLibrary(workspace, config.skill_paths).load(),
            selection=selection,
            session=session,
        )

        try:
            async for event in agent.run(job.prompt):
                if isinstance(event, Notice) and event.level == "error":
                    record.error = event.text
                elif isinstance(event, Done):
                    record.summary = event.text
                    if event.interrupted:
                        record.error = record.error or "interrupted"
        except asyncio.CancelledError:
            record.error = "cancelled"
            raise
        except Exception as error:  # noqa: BLE001
            record.error = f"{type(error).__name__}: {error}"
        finally:
            record.finished_at = time.time()
            record.cost = agent.state.total_cost
            record.ok = not record.error

        return record

    def _selection_for(self, job: ScheduledJob) -> Selection:
        if job.model:
            return Selection.parse(job.model, self.config)
        binding = self.config.role("main")
        if binding is None:
            raise NoRouteError("no 'main' role configured")
        return Selection.from_binding(binding)

    def _permissions_for(self, job: ScheduledJob, workspace: Path) -> PermissionEngine:
        """Build a permission engine for an unattended run.

        A scheduled job has nobody to answer an approval prompt, so ``ask``
        would simply stall. It is downgraded to ``auto``, which still refuses
        catastrophic commands.
        """
        import copy

        permission_config = copy.deepcopy(self.config.permissions)
        mode = job.permission_mode
        permission_config.mode = "auto" if mode == "ask" else mode
        return PermissionEngine(permission_config, workspace)

    def _config_for(self, job: ScheduledJob) -> Config:
        import copy

        config = copy.copy(self.config)
        config.max_agent_turns = job.max_turns
        return config

    async def run_now(self, job_id: str) -> RunRecord | None:
        """Run a job immediately, outside its schedule."""
        job = self.store.get(job_id)
        if job is None:
            return None
        await self._run_guarded(job)
        return job.last_record
