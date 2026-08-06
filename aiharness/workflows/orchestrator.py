"""Multi-phase orchestration: split, execute, review, repair, verify.

This is the heaviest workflow in the harness and the one worth understanding.
A single model working alone on a large change tends to fail in two ways: it
loses track of the parts it is not currently editing, and it grades its own
homework. The orchestrator addresses both by splitting the work across
independent agents and then handing the combined result to an agent whose
only job is to attack it.

Phases:

1. **Split** — a planner model turns the goal into disjoint assignments, each
   with its own file scope, so parallel workers do not collide.
2. **Execute** — assignments run as subagents, in dependency order, several
   at a time. Each may be pinned to a different model and API account.
3. **Review** — an adversarial model reads what was actually written and
   reports defects.
4. **Repair** — findings the review confirmed are handed back for fixing.
5. **Verify** — the project's checks run, and a reviewer rules on whether the
   original goal was met.

Every phase is optional and every phase's model is configurable, because the
right shape depends on the job.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..agent.subagent import SubagentResult, SubagentSpec, run_parallel, run_subagent
from ..constants import (
    DEFAULT_PARALLEL_AGENTS,
    REVIEW_MAX_TURNS,
    TASK_MAX_TURNS,
)
from ..providers.router import NoRouteError, Selection
from ..tools.base import ToolContext, ToolRegistry

ProgressHook = Callable[[str], None]

#: Tool sets granted to each kind of worker.
WRITER_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"]
READER_TOOLS = ["Read", "Glob", "Grep", "Bash"]

#: Guard against a planner that returns an unbounded task list.
MAX_ASSIGNMENTS = 12
#: Guard against a dependency cycle stalling the wave scheduler.
MAX_WAVES = 8


PLANNER_PROMPT = """\
You are splitting a software task into assignments for parallel agents.

Read enough of the codebase to split it correctly — guessing at the file
layout produces assignments that collide and waste every worker's effort.

Rules for a good split:

- Assignments must touch **disjoint sets of files**. Two agents editing the
  same file will overwrite each other.
- Each assignment must be independently completable from its own text. The
  worker cannot see this plan, the other assignments, or the user.
- State the file paths each assignment owns. If you cannot name them, the
  split is not ready — investigate further first.
- Use `depends_on` only for genuine ordering constraints (an interface must
  exist before its callers compile). Every dependency serialises the work, so
  do not add one for tidiness.
- Fewer, larger assignments beat many tiny ones. If the task is genuinely
  sequential, return a single assignment and say so.

Reply with JSON only, no prose and no code fence:

{"assignments": [
  {"id": "short-slug",
   "title": "one line",
   "instructions": "complete self-contained brief, including the acceptance criteria",
   "files": ["path/one.py"],
   "depends_on": []}
]}
"""

REVIEWER_PROMPT = """\
You are reviewing work that several agents just completed against a goal.

Read the files they changed. Do not review from their reports — a worker's
account of its own work is the least reliable evidence available.

Find, in priority order:

1. Places where the goal was not actually met, despite a report claiming it was.
2. Integration defects: mismatched signatures between the parts, duplicated
   logic, a caller never updated, an import that does not resolve.
3. Correctness bugs in the new code — wrong conditions, unhandled empty/null
   cases, resources never released, swallowed errors.
4. Security and data-loss risks.

For each defect:

  FINDING: <one sentence>
  WHERE: <path:line>
  FIX: <the specific change required>

Rank worst first. If the work is sound, say "NO BLOCKING ISSUES" and name the
riskiest assumption it rests on. Do not invent findings to appear thorough.
"""

REPAIR_PROMPT = """\
A reviewer found defects in work that was just completed. Fix them.

Apply the smallest change that genuinely resolves each finding. Read the code
around it before editing — the reviewer may have misread, and if a finding is
wrong, say so explicitly rather than making a pointless edit.

Do not refactor beyond the findings. Do not fix things nobody reported.
Report what you changed, file by file, and which findings you rejected and why.
"""


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass
class Assignment:
    """One unit of parallelisable work."""

    id: str
    title: str
    instructions: str
    files: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    model: str | None = None

    def brief(self) -> str:
        """The self-contained text handed to the worker."""
        text = f"{self.title}\n\n{self.instructions}"
        if self.files:
            owned = "\n".join(f"- {path}" for path in self.files)
            text += (
                f"\n\nYou own these files; do not modify anything outside them:\n{owned}"
            )
        return text


@dataclass
class PhaseReport:
    """Outcome of one orchestration phase."""

    name: str
    detail: str
    cost: float = 0.0
    ok: bool = True
    duration: float = 0.0


@dataclass
class OrchestrationResult:
    goal: str
    phases: list[PhaseReport] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(phase.cost for phase in self.phases)

    @property
    def ok(self) -> bool:
        return all(phase.ok for phase in self.phases)

    def render(self) -> str:
        """Format the run as a report for the calling agent."""
        lines = [f"# Orchestration — {'ok' if self.ok else 'issues found'}", ""]
        for phase in self.phases:
            status = "ok" if phase.ok else "FAILED"
            lines.append(
                f"## {phase.name} [{status}] "
                f"({phase.duration:.1f}s, ${phase.cost:.4f})"
            )
            lines.append("")
            lines.append(phase.detail.strip() or "(no output)")
            lines.append("")
        lines.append(f"_Total: ${self.total_cost:.4f} across {len(self.phases)} phase(s)._")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# planner output parsing
# --------------------------------------------------------------------------


def parse_assignments(text: str) -> list[Assignment]:
    """Extract assignments from a planner response.

    Tolerant of the fences and stray prose weaker models wrap JSON in.

    Args:
      text: Raw planner output.

    Returns:
      Parsed assignments, capped at :data:`MAX_ASSIGNMENTS`.

    Raises:
      ValueError: If no assignment list could be recovered.
    """
    payload = _extract_json_object(text)
    raw = payload.get("assignments")
    if not isinstance(raw, list) or not raw:
        raise ValueError("planner returned no assignments")

    assignments: list[Assignment] = []
    for index, item in enumerate(raw[:MAX_ASSIGNMENTS]):
        if not isinstance(item, dict):
            continue
        instructions = str(item.get("instructions") or "").strip()
        if not instructions:
            continue
        assignments.append(
            Assignment(
                id=str(item.get("id") or f"task-{index + 1}"),
                title=str(item.get("title") or f"Task {index + 1}").strip(),
                instructions=instructions,
                files=[str(f) for f in (item.get("files") or [])],
                depends_on=[str(d) for d in (item.get("depends_on") or [])],
                model=str(item["model"]) if item.get("model") else None,
            )
        )
    if not assignments:
        raise ValueError("planner returned no usable assignments")
    return assignments


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a possibly noisy response."""
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise ValueError("planner response was not JSON")


def schedule_waves(assignments: list[Assignment]) -> list[list[Assignment]]:
    """Group assignments into dependency-ordered waves.

    Args:
      assignments: The full assignment list.

    Returns:
      A list of waves; every assignment in a wave may run concurrently.
      Unsatisfiable dependencies are dropped into the final wave rather than
      deadlocking the run.
    """
    remaining = {a.id: a for a in assignments}
    done: set[str] = set()
    waves: list[list[Assignment]] = []

    while remaining and len(waves) < MAX_WAVES:
        ready = [
            a
            for a in remaining.values()
            if all(dep in done or dep not in remaining for dep in a.depends_on)
        ]
        if not ready:  # cycle or dangling dependency
            waves.append(list(remaining.values()))
            return waves
        waves.append(ready)
        for assignment in ready:
            done.add(assignment.id)
            del remaining[assignment.id]

    if remaining:
        waves.append(list(remaining.values()))
    return waves


# --------------------------------------------------------------------------
# the orchestrator
# --------------------------------------------------------------------------


class Orchestrator:
    """Runs the split/execute/review/repair/verify pipeline."""

    def __init__(
        self,
        ctx: ToolContext,
        registry: ToolRegistry,
        *,
        on_progress: ProgressHook | None = None,
    ):
        self.ctx = ctx
        self.registry = registry
        self.config = ctx.config
        self._progress = on_progress or (lambda line: None)

    # -- helpers ----------------------------------------------------------

    def _selection(self, spec: str | None, role: str) -> Selection:
        if spec:
            return Selection.parse(spec, self.config)
        binding = self.config.role(role)
        if binding is None:
            raise NoRouteError(f"role '{role}' is not configured")
        return Selection.from_binding(binding)

    def _note(self, line: str) -> None:
        self._progress(line)
        self.ctx.note(line)

    # -- phases -----------------------------------------------------------

    async def split(self, goal: str, *, model: str | None = None) -> tuple[list[Assignment], PhaseReport]:
        """Ask the planner to break the goal into disjoint assignments."""
        started = time.time()
        selection = self._selection(model, "main")
        self._note(f"planning with {selection.label()}")

        result = await run_subagent(
            SubagentSpec(
                prompt=f"Goal:\n\n{goal}",
                selection=selection,
                label="planner",
                system_prompt=self._system(PLANNER_PROMPT),
                max_turns=REVIEW_MAX_TURNS,
                tool_names=READER_TOOLS,
            ),
            self.ctx,
            self.registry,
            on_progress=lambda label, line: self._note(f"[{label}] {line}"),
        )

        try:
            assignments = parse_assignments(result.text)
        except ValueError as error:
            # A failed split is recoverable: run the whole goal as one unit.
            assignments = [
                Assignment(id="whole", title="Complete the goal", instructions=goal)
            ]
            detail = f"planner output unusable ({error}); running as a single assignment"
            return assignments, PhaseReport(
                "Split", detail, result.cost, ok=True, duration=time.time() - started
            )

        listing = "\n".join(
            f"- **{a.id}** {a.title}"
            + (f" — files: {', '.join(a.files)}" if a.files else "")
            + (f" — after: {', '.join(a.depends_on)}" if a.depends_on else "")
            for a in assignments
        )
        return assignments, PhaseReport(
            "Split",
            f"{len(assignments)} assignment(s):\n\n{listing}",
            result.cost,
            duration=time.time() - started,
        )

    async def execute(
        self,
        assignments: list[Assignment],
        *,
        model: str | None = None,
        parallel: int = DEFAULT_PARALLEL_AGENTS,
        read_only: bool = False,
    ) -> tuple[list[SubagentResult], PhaseReport]:
        """Run assignments as subagents, respecting declared dependencies."""
        started = time.time()
        tools = READER_TOOLS if read_only else WRITER_TOOLS
        collected: list[SubagentResult] = []

        for index, wave in enumerate(schedule_waves(assignments), start=1):
            specs = []
            for assignment in wave:
                selection = self._selection(assignment.model or model, "main")
                specs.append(
                    SubagentSpec(
                        prompt=assignment.brief(),
                        selection=selection,
                        label=assignment.id,
                        max_turns=TASK_MAX_TURNS,
                        tool_names=tools,
                    )
                )
            self._note(f"wave {index}: {len(specs)} agent(s) — " + ", ".join(s.label for s in specs))
            collected += await run_parallel(
                specs,
                self.ctx,
                self.registry,
                limit=max(parallel, 1),
                on_progress=lambda label, line: self._note(f"[{label}] {line}"),
            )

        failures = [r for r in collected if not r.ok]
        detail = "\n\n".join(
            f"### {r.label} ({r.selection})\n\n{r.text or '(no output)'}"
            + (f"\n\n_error: {r.error}_" if r.error else "")
            for r in collected
        )
        return collected, PhaseReport(
            "Execute",
            detail,
            sum(r.cost for r in collected),
            ok=not failures,
            duration=time.time() - started,
        )

    async def review(
        self, goal: str, results: list[SubagentResult], *, model: str | None = None
    ) -> tuple[str, PhaseReport]:
        """Have an adversarial model attack the combined result."""
        started = time.time()
        selection = self._selection(model, self.config.workflows.adversarial.adversary_role)
        self._note(f"adversarial review with {selection.label()}")

        reports = "\n\n".join(f"### {r.label}\n{r.text}" for r in results if r.text)
        result = await run_subagent(
            SubagentSpec(
                prompt=(
                    f"Goal:\n\n{goal}\n\n"
                    f"What the workers claim they did:\n\n{reports}"
                ),
                selection=selection,
                label="reviewer",
                system_prompt=self._system(REVIEWER_PROMPT),
                max_turns=REVIEW_MAX_TURNS,
                tool_names=READER_TOOLS,
            ),
            self.ctx,
            self.registry,
            on_progress=lambda label, line: self._note(f"[{label}] {line}"),
        )
        findings = result.text.strip()
        clean = "NO BLOCKING ISSUES" in findings.upper()
        return findings, PhaseReport(
            "Review",
            findings or "(reviewer produced nothing)",
            result.cost,
            ok=clean,
            duration=time.time() - started,
        )

    async def repair(
        self, goal: str, findings: str, *, model: str | None = None
    ) -> PhaseReport:
        """Fix the defects the review reported."""
        started = time.time()
        selection = self._selection(model, "main")
        self._note(f"repairing with {selection.label()}")

        result = await run_subagent(
            SubagentSpec(
                prompt=f"Original goal:\n\n{goal}\n\nReview findings:\n\n{findings}",
                selection=selection,
                label="repair",
                system_prompt=self._system(REPAIR_PROMPT),
                max_turns=TASK_MAX_TURNS,
                tool_names=WRITER_TOOLS,
            ),
            self.ctx,
            self.registry,
            on_progress=lambda label, line: self._note(f"[{label}] {line}"),
        )
        return PhaseReport(
            "Repair",
            result.text or "(no output)",
            result.cost,
            ok=result.ok,
            duration=time.time() - started,
        )

    def _system(self, extra: str) -> str:
        from ..agent.prompts import build_system_prompt
        from ..tools.shell import find_shell

        _, _, dialect = find_shell()
        return build_system_prompt(
            self.ctx.workspace,
            shell=dialect,
            extra=extra,
            permission_mode=self.ctx.permissions.mode,
        )

    # -- top level --------------------------------------------------------

    async def run(
        self,
        goal: str,
        *,
        worker_model: str | None = None,
        planner_model: str | None = None,
        reviewer_model: str | None = None,
        parallel: int = DEFAULT_PARALLEL_AGENTS,
        review_rounds: int = 1,
        repair: bool = True,
        read_only: bool = False,
    ) -> OrchestrationResult:
        """Run the full pipeline.

        Args:
          goal: What to accomplish, stated concretely.
          worker_model: Model spec for the execution agents.
          planner_model: Model spec for the split phase.
          reviewer_model: Model spec for the adversarial review.
          parallel: Maximum concurrent workers.
          review_rounds: How many review/repair cycles to run.
          repair: Whether to act on review findings.
          read_only: Deny write tools to every phase (dry run).

        Returns:
          The accumulated :class:`OrchestrationResult`.
        """
        outcome = OrchestrationResult(goal=goal)

        assignments, split_report = await self.split(goal, model=planner_model)
        outcome.assignments = assignments
        outcome.phases.append(split_report)

        results, execute_report = await self.execute(
            assignments, model=worker_model, parallel=parallel, read_only=read_only
        )
        outcome.phases.append(execute_report)

        for round_index in range(max(review_rounds, 0)):
            findings, review_report = await self.review(
                goal, results, model=reviewer_model
            )
            review_report.name = f"Review {round_index + 1}"
            outcome.phases.append(review_report)

            if review_report.ok or not findings:
                break
            if not repair or read_only:
                break

            repair_report = await self.repair(goal, findings, model=worker_model)
            repair_report.name = f"Repair {round_index + 1}"
            outcome.phases.append(repair_report)

        return outcome
