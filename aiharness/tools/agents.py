"""Tools that spawn other models: Delegate, Task, Research."""

from __future__ import annotations

from typing import Any

from ..agent.subagent import SubagentSpec, run_parallel, run_subagent
from ..constants import AGENT_LABEL_CHARS, DELEGATE_MAX_TURNS, TASK_MAX_TURNS
from ..providers.router import NoRouteError, Selection
from .base import Tool, ToolContext, ToolResult

#: Tool sets granted to spawned agents.
READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "Bash"]
WRITE_TOOLS = ["Write", "Edit"]


def _resolve_selection(spec: str | None, ctx: ToolContext, fallback_role: str) -> Selection:
    """Accept ``model``, ``model@account``, ``role:name``, or nothing."""
    if spec:
        return Selection.parse(spec, ctx.config)
    binding = ctx.config.role(fallback_role)
    if not binding:
        raise NoRouteError(f"role '{fallback_role}' is not configured")
    return Selection.from_binding(binding)


def _format_result(r: Any, *, show_cost: bool = True) -> str:
    header = f"### {r.label} ({r.selection})"
    if r.error and not r.text:
        return f"{header}\n\nFAILED: {r.error}"
    footer = ""
    if show_cost:
        footer = (
            f"\n\n_[{r.turns} turn(s), {r.tool_calls} tool call(s), "
            f"{r.usage.total:,} tokens, ${r.cost:.4f}]_"
        )
    warn = f"\n\n_(partial: {r.error})_" if r.error else ""
    return f"{header}\n\n{r.text or '(no output)'}{warn}{footer}"


class DelegateTool(Tool):
    name = "Delegate"
    subagent_safe = False
    bulky = True
    description = """
Run a self-contained subtask on a cheaper/faster model and get its result.

Use it for work that does not need your full capability: mechanical edits
across many files, summarising a long file, extracting data, drafting
boilerplate or commit messages, triaging which files are relevant.

The subagent cannot see this conversation — put everything it needs in
`task`, including exact file paths, the expected output format, and any
constraint that matters. You remain responsible for checking what it returns.

By default it runs on the "cheap" role; pass `model` to override with a
specific model, a `model@account` pin, or `role:fast`.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Complete, self-contained instructions including file paths and the output format you want",
                },
                "model": {
                    "type": "string",
                    "description": "Optional: 'model', 'model@account', or 'role:cheap'/'role:fast'",
                },
                "read_only": {
                    "type": "boolean",
                    "description": "Deny the subagent write access (default false)",
                },
                "max_turns": {"type": "integer", "description": "Turn budget, default 15"},
            },
            "required": ["task"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry

        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult.error("task is empty")

        delegation = ctx.config.workflows.delegation
        if not delegation.enabled:
            return ToolResult.error("delegation is disabled in config")

        try:
            selection = _resolve_selection(args.get("model"), ctx, delegation.cheap_role)
        except NoRouteError as e:
            return ToolResult.error(str(e))

        registry = build_registry(include_agent_tools=False)
        names = list(READ_ONLY_TOOLS)
        if not args.get("read_only"):
            names += WRITE_TOOLS

        ctx.note(f"delegating to {selection.label()}")
        result = await run_subagent(
            SubagentSpec(
                prompt=task,
                selection=selection,
                label="delegate",
                max_turns=int(args.get("max_turns") or DELEGATE_MAX_TURNS),
                tool_names=names,
            ),
            ctx,
            registry,
            on_progress=lambda label, line: ctx.note(f"[{label}] {line}"),
        )
        if not result.ok and not result.text:
            return ToolResult.error(f"delegate ({selection.label()}) failed: {result.error}")
        return ToolResult(
            content=_format_result(result),
            summary=f"delegated to {selection.label()} — ${result.cost:.4f}",
            display={"kind": "delegate", "model": selection.label()},
        )


class TaskTool(Tool):
    name = "Task"
    subagent_safe = False
    bulky = True
    description = """
Launch one subagent with an explicitly chosen model and account.

Unlike Delegate (which targets the cheap model), Task lets you pick exactly
which model and which API account runs the work — useful when a particular
model is better at a particular thing, or when you want to spread load across
accounts. The subagent starts with no knowledge of this conversation.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Self-contained task description"},
                "model": {
                    "type": "string",
                    "description": "'model', 'model@account', or 'role:name'. Defaults to the main model.",
                },
                "label": {"type": "string", "description": "Short name shown in the transcript"},
                "read_only": {"type": "boolean"},
                "max_turns": {"type": "integer"},
            },
            "required": ["prompt"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry

        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult.error("prompt is empty")
        try:
            selection = _resolve_selection(args.get("model"), ctx, "main")
        except NoRouteError as e:
            return ToolResult.error(str(e))

        registry = build_registry(include_agent_tools=False)
        names = [*READ_ONLY_TOOLS, "Skill"]
        if not args.get("read_only"):
            names += WRITE_TOOLS

        label = str(args.get("label") or selection.model_id)
        ctx.note(f"subagent '{label}' on {selection.label()}")
        result = await run_subagent(
            SubagentSpec(
                prompt=prompt,
                selection=selection,
                label=label,
                max_turns=int(args.get("max_turns") or TASK_MAX_TURNS),
                tool_names=names,
            ),
            ctx,
            registry,
            on_progress=lambda lbl, line: ctx.note(f"[{lbl}] {line}"),
        )
        if not result.ok and not result.text:
            return ToolResult.error(f"subagent failed: {result.error}")
        return ToolResult(
            content=_format_result(result),
            summary=f"{label} finished — ${result.cost:.4f}",
        )


SYNTHESIS_PROMPT = """\
Several independent investigators looked at the same question using different \
models. Their reports follow.

Produce one synthesised answer:

- Lead with the answer to the question, not with a description of the reports.
- State what they agree on as established.
- Where they disagree, say so explicitly, name which report claimed what, and \
say which is better supported by concrete evidence (file paths, code, command \
output) rather than by assertion.
- Drop claims that no report backed with evidence.
- List anything that remains genuinely unresolved.

Do not average the reports into vagueness, and do not pad. Cite file paths and \
line numbers wherever the reports supplied them.
"""


class ResearchTool(Tool):
    name = "Research"
    subagent_safe = False
    bulky = True
    description = """
Investigate a question with several models in parallel, then synthesise.

Each subagent gets the same question and works independently with read-only
tools; a synthesis pass reconciles their findings and flags disagreement.

Use for open-ended investigation where one model's blind spot is expensive:
understanding an unfamiliar codebase, tracing where behaviour comes from,
comparing design options, or auditing for a class of bug. It costs several
model calls — do not use it for questions a single Grep would answer.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to investigate, with enough context to work from",
                },
                "angles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional distinct angles, one per investigator (e.g. 'check the data layer', 'check the API surface')",
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit models/accounts, e.g. ['ds-chat@deepseek-a', 'kimi@openrouter']",
                },
                "synthesise": {"type": "boolean", "description": "Merge the reports (default true)"},
            },
            "required": ["question"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry

        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult.error("question is empty")

        rcfg = ctx.config.workflows.research
        angles = [str(a) for a in (args.get("angles") or []) if str(a).strip()]

        # Resolve the roster: explicit models > configured models > researcher role.
        selections: list[Selection] = []
        try:
            for spec in args.get("models") or []:
                selections.append(Selection.parse(str(spec), ctx.config))
            if not selections:
                for binding in rcfg.models:
                    if ctx.config.model(binding.model):
                        selections.append(Selection.from_binding(binding))
            if not selections:
                selections = [_resolve_selection(None, ctx, "researcher")]
        except NoRouteError as e:
            return ToolResult.error(str(e))

        n = max(len(angles), 1) if angles else min(rcfg.parallel, len(selections)) or 1
        specs: list[SubagentSpec] = []
        for i in range(n):
            selection = selections[i % len(selections)]
            angle = angles[i] if i < len(angles) else ""
            prompt = question
            if angle:
                prompt = f"{question}\n\nYour assigned angle: {angle}\nCover it thoroughly; other investigators cover the rest."
            specs.append(
                SubagentSpec(
                    prompt=prompt,
                    selection=selection,
                    label=angle[:AGENT_LABEL_CHARS] or f"investigator-{i + 1}",
                    max_turns=rcfg.max_turns,
                    tool_names=list(READ_ONLY_TOOLS),
                )
            )

        ctx.note(f"researching with {len(specs)} agent(s): " + ", ".join(s.selection.label() for s in specs))
        results = await run_parallel(
            specs,
            ctx,
            build_registry(include_agent_tools=False),
            limit=max(rcfg.parallel, 1),
            on_progress=lambda label, line: ctx.note(f"[{label}] {line}"),
        )

        reports = "\n\n".join(_format_result(r, show_cost=False) for r in results)
        total_cost = sum(r.cost for r in results)
        succeeded = sum(1 for r in results if r.ok and r.text)

        if not succeeded:
            return ToolResult.error(
                "every investigator failed:\n"
                + "\n".join(f"- {r.label}: {r.error}" for r in results)
            )

        if args.get("synthesise") is False or len(results) == 1:
            return ToolResult(
                content=reports,
                summary=f"{succeeded}/{len(results)} investigator(s) — ${total_cost:.4f}",
            )

        try:
            synth_selection = _resolve_selection(None, ctx, rcfg.synthesis_role)
            from ..providers.base import Message

            reply = await ctx.router.ask(
                synth_selection,
                [
                    Message(role="system", content=SYNTHESIS_PROMPT),
                    Message(
                        role="user",
                        content=f"Question:\n{question}\n\nReports:\n\n{reports}",
                    ),
                ],
                role="research-synthesis",
            )
            synthesis = reply.message.content.strip()
        except Exception as error:  # noqa: BLE001 - partial results still help
            return ToolResult(
                content=f"{reports}\n\n_(synthesis failed: {error})_",
                summary=f"{succeeded} report(s), synthesis failed",
            )

        return ToolResult(
            content=(
                f"## Synthesis\n\n{synthesis}\n\n---\n\n<details>\n"
                f"## Individual reports\n\n{reports}\n</details>"
            ),
            summary=f"{succeeded} investigator(s) synthesised — ${total_cost:.4f}",
            display={"kind": "research", "agents": len(results)},
        )
