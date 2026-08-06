"""Tools that hand control back to the user: questions and plans.

Everything else the agent does is autonomous. These two are the points where
it stops and waits, and both exist because guessing is expensive: guessing an
ambiguous requirement wastes a build, and guessing an approach wastes a
review cycle.
"""

from __future__ import annotations

from typing import Any

from ..agent.planning import Plan, PlanStep, parse_questions
from ..constants import MAX_CLARIFYING_QUESTIONS, MAX_PLAN_STEPS, MAX_QUESTION_OPTIONS
from .base import Tool, ToolContext, ToolResult


class AskUserTool(Tool):
    """Asks the user to decide something the agent cannot decide itself."""

    name = "AskUser"
    subagent_safe = False
    description = f"""
Ask the user to choose between options.

Use this only when the answer changes what you build and you cannot resolve
it yourself. Before asking, check whether the codebase already answers it —
an existing convention beats a question. Do not ask about style, do not ask
for permission to start, and do not ask about choices with an obvious
default: pick the default, say you picked it, and carry on.

Good reasons to ask: two readings of the request lead to materially different
work; a decision that is expensive to reverse; a missing constraint only the
user knows (deployment target, existing system to match, deadline).

Give at most {MAX_CLARIFYING_QUESTIONS} questions and {MAX_QUESTION_OPTIONS}
options each. The interface adds an "other" choice, so do not write one. If
you recommend an option, put it first and mark it "(recommended)" (or the
equivalent in the user's language).

Write every question, header, option label, and description in the **same
language the user has been using** in this conversation. Do not ask in
English when the user writes Chinese (or another language).
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "Ends with a question mark"},
                            "header": {"type": "string", "description": "Chip label, max 12 chars"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {
                                            "type": "string",
                                            "description": "What this choice means, or its trade-off",
                                        },
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "multi_select": {"type": "boolean"},
                        },
                        "required": ["question", "header", "options"],
                    },
                }
            },
            "required": ["questions"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        questions = parse_questions(args.get("questions"))
        if not questions:
            return ToolResult.error(
                "no usable questions: each needs a question, a header and at least "
                "two options with descriptions"
            )
        if ctx.ask_user is None:
            return ToolResult.error(
                "no interactive channel available (headless run). Choose the most "
                "reasonable option yourself, state the assumption, and continue."
            )

        answers = await ctx.ask_user(questions)
        if not answers:
            return ToolResult.error(
                "the user dismissed the questions. Proceed with the most reasonable "
                "default and say which assumption you made."
            )

        rendered = "\n".join(f"- **{header}**: {answer}" for header, answer in answers.items())
        return ToolResult(
            content=f"The user answered:\n\n{rendered}",
            summary=f"asked {len(questions)} question(s)",
            display={"kind": "ask_user", "answers": answers},
        )


class PresentPlanTool(Tool):
    """Submits a plan for approval and blocks until the user rules on it."""

    name = "PresentPlan"
    subagent_safe = False
    bulky = True
    description = f"""
Present an implementation plan and wait for the user to approve or change it.

Call this when you are in plan mode and have investigated enough to commit to
an approach. Until it is approved, writes are blocked — that is the point of
plan mode, so do not try to work around it.

A plan is worth reading only if it is specific: name the real files, say what
changes in each, and say what you are deliberately *not* doing. At most
{MAX_PLAN_STEPS} steps; if you need more, your steps are too small.

The user may approve it, or send feedback — in which case revise and present
again. Do not argue with feedback that is a preference; do push back once, in
one sentence, if feedback rests on a mistaken premise about the code.

Write the plan (goal, steps, risks, open questions, out of scope) in the
**same language the user has been using**. Do not default to English when
they write Chinese or another language.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What will be true when this is done, in the user's terms",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Imperative, one line"},
                            "detail": {"type": "string"},
                            "files": {"type": "array", "items": {"type": "string"}},
                            "model": {
                                "type": "string",
                                "description": "Optional model for this step, e.g. 'ds-chat@deepseek-b'",
                            },
                        },
                        "required": ["title"],
                    },
                },
                "risks": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "out_of_scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What you are deliberately not doing",
                },
            },
            "required": ["goal", "steps"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        goal = str(args.get("goal", "")).strip()
        if not goal:
            return ToolResult.error("the plan needs a goal")

        steps: list[PlanStep] = []
        for item in (args.get("steps") or [])[:MAX_PLAN_STEPS]:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            steps.append(
                PlanStep(
                    title=str(item["title"]).strip(),
                    detail=str(item.get("detail") or "").strip(),
                    files=[str(f) for f in (item.get("files") or [])],
                    model=str(item.get("model") or ""),
                )
            )
        if not steps:
            return ToolResult.error("the plan needs at least one step")

        def strings(key: str) -> list[str]:
            return [str(v).strip() for v in (args.get(key) or []) if str(v).strip()]

        plan = Plan(
            goal=goal,
            steps=steps,
            risks=strings("risks"),
            open_questions=strings("open_questions"),
            out_of_scope=strings("out_of_scope"),
            revision=ctx.plan_revision + 1,
        )

        if ctx.present_plan is None:
            # Headless: record the plan and proceed. Blocking forever with
            # nobody to answer would be worse than acting on a stated plan.
            ctx.plan = plan
            return ToolResult(
                content=(
                    f"No interactive channel; proceeding on this plan without approval.\n\n"
                    f"{plan.render(chinese=False)}"
                ),
                summary=f"plan recorded ({plan.summary_line()})",
            )

        approved, feedback = await ctx.present_plan(plan)
        ctx.plan = plan
        ctx.plan_revision = plan.revision

        if approved:
            return ToolResult(
                content=(
                    "The user approved the plan. Writes are unblocked. Execute it "
                    "step by step, keeping the todo list current, and report any "
                    "point where reality turns out to differ from the plan.\n\n"
                    f"{plan.render(chinese=False)}"
                ),
                summary=f"plan approved — {plan.summary_line()}",
                display={"kind": "plan", "approved": True},
            )

        return ToolResult(
            content=(
                f"The user did not approve it yet. Their feedback:\n\n{feedback}\n\n"
                "Revise and present again. Do not start writing files."
            ),
            summary="plan sent back for revision",
            display={"kind": "plan", "approved": False},
        )
