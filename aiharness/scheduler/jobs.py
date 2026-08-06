"""Scheduled job definitions and their persistence."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from ..constants import SCHEDULER_HISTORY_LIMIT, SCHEDULER_JOB_MAX_TURNS
from .cron import Schedule

JOBS_FILE = "jobs.json"


def jobs_path() -> Path:
    """Return the file holding the scheduled-job definitions."""
    override = os.environ.get("AIH_JOBS_FILE")
    if override:
        return Path(override).expanduser()
    return Path(user_data_dir("aiharness", appauthor=False)) / JOBS_FILE


@dataclass
class RunRecord:
    """One execution of a job."""

    started_at: float
    finished_at: float = 0.0
    ok: bool = False
    session_id: str = ""
    summary: str = ""
    error: str = ""
    cost: float = 0.0

    @property
    def duration(self) -> float:
        if not self.finished_at:
            return 0.0
        return self.finished_at - self.started_at

    @property
    def started_label(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started_at))


@dataclass
class ScheduledJob:
    """A prompt the harness runs on a schedule, unattended."""

    id: str
    name: str
    prompt: str
    workspace: str
    schedule: Schedule = field(default_factory=Schedule)
    enabled: bool = True
    #: Model spec: ``model``, ``model@account`` or ``role:name``. Empty = main.
    model: str = ""
    #: Permission mode used for this run. Unattended jobs cannot answer
    #: prompts, so anything but ``yolo`` will stall on an approval request —
    #: which is why ``auto`` is the default and the UI warns about ``ask``.
    permission_mode: str = "auto"
    max_turns: int = SCHEDULER_JOB_MAX_TURNS
    #: Run the job's own verification pass before reporting success.
    verify: bool = False
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    next_run: float = 0.0
    history: list[RunRecord] = field(default_factory=list)

    # -- scheduling -------------------------------------------------------

    def compute_next_run(self, after: datetime | None = None) -> float:
        """Recompute and store :attr:`next_run`.

        Returns:
          The next run as epoch seconds, or ``0.0`` if it will never run again.
        """
        reference = after or datetime.now()
        moment = self.schedule.next_after(reference, last_run=self.last_run)
        self.next_run = moment.timestamp() if moment else 0.0
        return self.next_run

    def is_due(self, now: float, grace: float) -> bool:
        """Whether the job should run at ``now``.

        Args:
          now: Current epoch seconds.
          grace: How long after the due time a missed run is still honoured,
            so a job does not silently vanish because the app was closed.
        """
        if not self.enabled or not self.next_run:
            return False
        return self.next_run <= now <= self.next_run + grace

    def is_overdue(self, now: float, grace: float) -> bool:
        """Whether the due time passed without the job running."""
        return bool(self.enabled and self.next_run and now > self.next_run + grace)

    # -- history ----------------------------------------------------------

    def record_run(self, record: RunRecord) -> None:
        self.history.append(record)
        del self.history[:-SCHEDULER_HISTORY_LIMIT]
        self.last_run = record.started_at

    @property
    def last_record(self) -> RunRecord | None:
        return self.history[-1] if self.history else None

    # -- display ----------------------------------------------------------

    def next_run_label(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.next_run:
            return "never"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.next_run))

    def describe(self, chinese: bool = False) -> str:
        return self.schedule.describe(chinese=chinese)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schedule"] = asdict(self.schedule)
        payload["history"] = [asdict(record) for record in self.history]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScheduledJob:
        raw_schedule = payload.get("schedule") or {}
        schedule = Schedule(
            kind=raw_schedule.get("kind", "daily"),
            times=list(raw_schedule.get("times") or ["09:00"]),
            weekdays=list(raw_schedule.get("weekdays") or []),
            interval_minutes=int(raw_schedule.get("interval_minutes") or 0),
            cron=raw_schedule.get("cron", ""),
            at=float(raw_schedule.get("at") or 0.0),
        )
        history = [
            RunRecord(**{k: v for k, v in record.items() if k in RunRecord.__annotations__})
            for record in payload.get("history") or []
        ]
        return cls(
            id=payload.get("id") or uuid.uuid4().hex[:8],
            name=payload.get("name", "job"),
            prompt=payload.get("prompt", ""),
            workspace=payload.get("workspace", "."),
            schedule=schedule,
            enabled=bool(payload.get("enabled", True)),
            model=payload.get("model", ""),
            permission_mode=payload.get("permission_mode", "auto"),
            max_turns=int(payload.get("max_turns") or SCHEDULER_JOB_MAX_TURNS),
            verify=bool(payload.get("verify", False)),
            created_at=float(payload.get("created_at") or time.time()),
            last_run=float(payload.get("last_run") or 0.0),
            next_run=float(payload.get("next_run") or 0.0),
            history=history,
        )


class JobStore:
    """Loads and saves the job list as a single JSON document."""

    def __init__(self, path: Path | None = None):
        self.path = path or jobs_path()
        self._jobs: dict[str, ScheduledJob] = {}
        self.load()

    def load(self) -> JobStore:
        self._jobs.clear()
        if not self.path.is_file():
            return self
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self
        for record in payload.get("jobs") or []:
            try:
                job = ScheduledJob.from_dict(record)
            except (TypeError, ValueError):
                continue
            self._jobs[job.id] = job
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": [job.to_dict() for job in self._jobs.values()]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- CRUD -------------------------------------------------------------

    def add(
        self,
        name: str,
        prompt: str,
        schedule: Schedule,
        workspace: Path,
        **options: Any,
    ) -> ScheduledJob:
        """Create a job and persist it.

        Raises:
          ScheduleError: If the schedule is not usable.
        """
        schedule.validate()
        job = ScheduledJob(
            id=uuid.uuid4().hex[:8],
            name=name.strip() or "job",
            prompt=prompt,
            workspace=str(workspace),
            schedule=schedule,
            **options,
        )
        job.compute_next_run()
        self._jobs[job.id] = job
        self.save()
        return job

    def get(self, job_id: str) -> ScheduledJob | None:
        if job_id in self._jobs:
            return self._jobs[job_id]
        # Allow addressing a job by name when it is unambiguous.
        matches = [job for job in self._jobs.values() if job.name == job_id]
        return matches[0] if len(matches) == 1 else None

    def all(self) -> list[ScheduledJob]:
        return sorted(self._jobs.values(), key=lambda job: (not job.enabled, job.next_run or 0))

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        del self._jobs[job.id]
        self.save()
        return True

    def remove_all(self) -> int:
        count = len(self._jobs)
        self._jobs.clear()
        self.save()
        return count

    def set_enabled(self, job_id: str, enabled: bool) -> ScheduledJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.enabled = enabled
        if enabled:
            job.compute_next_run()
        self.save()
        return job

    def update(self, job: ScheduledJob) -> None:
        self._jobs[job.id] = job
        self.save()
