"""Deciding how much ceremony a request deserves, and drafting plans.

Two failure modes bracket this problem. Treat every request as a project and
"rename this variable" turns into a five-phase plan nobody asked for. Treat
every request as a chore and "rewrite the auth layer" starts editing files
before anyone has agreed what it should look like.

So each new request is classified first — cheaply, on the ``fast`` model —
into one of three bands, and the harness routes accordingly:

``TRIVIAL``
  Answer it. No todo list, no plan, no questions.
``SIMPLE``
  Do it directly, with a todo list if there are several steps.
``PROJECT``
  Ask whatever is genuinely unclear, draft a plan, get it agreed, then build.

When the classifier is unsure, or the request has a real fork in it, the
right move is to ask rather than to guess — but only about things where the
answer changes what gets built.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..constants import (
    MAX_CLARIFYING_QUESTIONS,
    MAX_PLAN_STEPS,
    MAX_QUESTION_OPTIONS,
    MIN_QUESTION_OPTIONS,
    PROJECT_COMPLEXITY_THRESHOLD,
    TRIVIAL_COMPLEXITY_THRESHOLD,
)
from ..providers.base import Message

if TYPE_CHECKING:  # pragma: no cover
    from ..providers.router import Router, Selection


class Complexity(Enum):
    """How much process a request warrants."""

    TRIVIAL = "trivial"
    SIMPLE = "simple"
    PROJECT = "project"

    @property
    def label_zh(self) -> str:
        return {"trivial": "小问题", "simple": "常规任务", "project": "项目"}[self.value]


@dataclass
class Question:
    """One clarifying question with pre-baked options.

    Options exist because they are far cheaper for the user to answer than
    free text, and because writing them forces the asker to have actually
    thought about the alternatives. "Other" is always available and is not
    listed here — the UI adds it.
    """

    question: str
    header: str  # short chip label, <= 12 chars
    options: list[dict[str, str]] = field(default_factory=list)  # {label, description}
    multi_select: bool = False

    def option_labels(self) -> list[str]:
        return [str(option.get("label", "")) for option in self.options]


@dataclass
class Classification:
    """The verdict on one request."""

    complexity: Complexity
    score: int
    reason: str = ""
    questions: list[Question] = field(default_factory=list)

    @property
    def needs_plan(self) -> bool:
        return self.complexity is Complexity.PROJECT

    @property
    def needs_clarification(self) -> bool:
        return bool(self.questions)


@dataclass
class PlanStep:
    """One unit of work in a plan."""

    title: str
    detail: str = ""
    files: list[str] = field(default_factory=list)
    #: Optional model override, so a plan can assign work to specific models.
    model: str = ""

    def render(self, index: int) -> str:
        line = f"{index}. **{self.title}**"
        if self.detail:
            line += f"\n   {self.detail}"
        if self.files:
            line += f"\n   files: {', '.join(self.files)}"
        if self.model:
            line += f"\n   model: {self.model}"
        return line


@dataclass
class Plan:
    """A proposal the user can argue with before anything is written."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    revision: int = 1
    approved: bool = False

    def render(self, chinese: bool = True) -> str:
        """Format the plan for display and for pinning into context."""
        if chinese:
            heads = ("目标", "步骤", "风险", "待确认", "不做的", "修订")
        else:
            heads = ("Goal", "Steps", "Risks", "Open questions", "Out of scope", "revision")

        parts = [f"## {heads[0]}\n\n{self.goal}"]
        if self.steps:
            body = "\n\n".join(step.render(i) for i, step in enumerate(self.steps, 1))
            parts.append(f"## {heads[1]}\n\n{body}")
        for head, items in ((heads[2], self.risks), (heads[3], self.open_questions),
                            (heads[4], self.out_of_scope)):
            if items:
                parts.append(f"## {head}\n\n" + "\n".join(f"- {item}" for item in items))
        parts.append(f"_{heads[5]} {self.revision}_")
        return "\n\n".join(parts)

    def summary_line(self) -> str:
        return f"{len(self.steps)} step(s), revision {self.revision}"


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

CLASSIFIER_PROMPT = """\
Classify one request to a coding agent. Reply with JSON only — no prose, no
code fence.

{"score": <1-10>, "reason": "<one short sentence>", "questions": [...]}

`score` is how much process the request deserves:

  1-2  A question, a one-line change, a lookup. Just do it.
  3-4  A normal task: a few files, a clear goal, no design decisions.
  5-7  Real work: several components, or a decision that is expensive to
       reverse, or something touching auth, money, migrations or deletion.
  8-10 A project: new subsystem, broad refactor, or a goal stated so loosely
       that two competent people would build different things.

Judge the *work*, not the wording. "Add a button" to a codebase with no UI
layer is not a 2. "Refactor everything" that turns out to be one file is not
an 8.

`questions` holds only the things you genuinely cannot decide yourself and
where a wrong guess means building the wrong thing. Leave it empty for
anything you could resolve by reading the code, and for choices with an
obvious conventional default. Never ask about style preferences, and never
ask more than %(max_questions)d.

Each question:

  {"question": "<full question ending in ?>",
   "header": "<= 12 chars",
   "options": [{"label": "<1-5 words>", "description": "<what this means / the trade-off>"}],
   "multi_select": false}

Give %(max_options)d options at most. Do not include an "other" option; the
interface adds one. If you recommend one, put it first and append
" (recommended)" to its label — or the local equivalent when not writing
English.

**Language:** write `reason`, every `question`, `header`, option `label`, and
`description` in the **same language as the user's request**. If the user
wrote Chinese, ask in Chinese; if English, ask in English. Do not default to
English when the request is not English.
"""

PLANNER_PROMPT = """\
Draft an implementation plan. Investigate first with the tools you have — a
plan written without reading the code is a guess with formatting.

Reply with JSON only, no prose, no code fence:

{"goal": "<what will be true when this is done, in the user's terms>",
 "steps": [{"title": "<imperative, one line>",
            "detail": "<what actually changes and why>",
            "files": ["path/one.py"],
            "model": ""}],
 "risks": ["<what could go wrong, concretely>"],
 "open_questions": ["<what you could not determine>"],
 "out_of_scope": ["<what you are deliberately not doing>"]}

Rules:

- Name real files. If you cannot name them, you have not investigated enough.
- Each step must be independently checkable: someone else should be able to
  tell whether it is done.
- Order by dependency, not by importance.
- `risks` means specific failure modes, not "this might be complex".
- `out_of_scope` protects the user from scope creep. Use it whenever the
  request could reasonably be read more broadly than you are reading it.
- At most %(max_steps)d steps. If you need more, the steps are too small.
- Set `model` only where a specific model genuinely suits that step better;
  leave it empty otherwise.
- Write `goal`, step titles/details, risks, open_questions, and out_of_scope
  in the **same language as the user's request**.
"""

REVISION_PROMPT = """\
Revise the plan below according to the user's feedback. Reply with the same
JSON shape, complete — not a diff.

Take the feedback seriously rather than politely: if the user is redirecting
the approach, change the approach, do not re-label the old one. If part of
their feedback rests on a mistaken premise about the code, say so in
`open_questions` and plan for what is actually there.
"""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response.

    Raises:
      ValueError: If nothing parseable is present.
    """
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
    raise ValueError("response was not JSON")


def parse_questions(raw: Any) -> list[Question]:
    """Build questions from classifier output, dropping malformed ones."""
    if not isinstance(raw, list):
        return []
    questions: list[Question] = []
    for item in raw[:MAX_CLARIFYING_QUESTIONS]:
        if not isinstance(item, dict) or not item.get("question"):
            continue
        options = []
        for option in (item.get("options") or [])[:MAX_QUESTION_OPTIONS]:
            if isinstance(option, dict) and option.get("label"):
                options.append(
                    {
                        "label": str(option["label"]),
                        "description": str(option.get("description", "")),
                    }
                )
        if len(options) < MIN_QUESTION_OPTIONS:
            continue  # a question with one answer is not a question
        questions.append(
            Question(
                question=str(item["question"]),
                header=str(item.get("header") or "Choice")[:12],
                options=options,
                multi_select=bool(item.get("multi_select")),
            )
        )
    return questions


def score_to_complexity(score: int) -> Complexity:
    if score >= PROJECT_COMPLEXITY_THRESHOLD:
        return Complexity.PROJECT
    if score <= TRIVIAL_COMPLEXITY_THRESHOLD:
        return Complexity.TRIVIAL
    return Complexity.SIMPLE


def parse_plan(raw: dict[str, Any], fallback_goal: str) -> Plan:
    """Build a :class:`Plan` from planner output."""
    steps: list[PlanStep] = []
    for item in (raw.get("steps") or [])[:MAX_PLAN_STEPS]:
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

    def strings(key: str) -> list[str]:
        return [str(v).strip() for v in (raw.get(key) or []) if str(v).strip()]

    return Plan(
        goal=str(raw.get("goal") or fallback_goal).strip(),
        steps=steps,
        risks=strings("risks"),
        open_questions=strings("open_questions"),
        out_of_scope=strings("out_of_scope"),
    )


# --------------------------------------------------------------------------
# model calls
# --------------------------------------------------------------------------


async def classify_request(
    request: str,
    router: Router,
    selection: Selection,
    *,
    context: str = "",
) -> Classification:
    """Decide how much process a request deserves.

    Args:
      request: What the user asked for.
      router: Router used to reach the classifier model.
      selection: Which model classifies.
      context: Optional extra context, e.g. what the project is.

    Returns:
      A :class:`Classification`. On any failure the request is treated as
      ``SIMPLE`` — the middle band — because refusing to proceed because the
      classifier broke would be worse than skipping the ceremony.
    """
    prompt = CLASSIFIER_PROMPT % {
        "max_questions": MAX_CLARIFYING_QUESTIONS,
        "max_options": MAX_QUESTION_OPTIONS,
    }
    user = f"Request:\n{request}"
    if context:
        user += f"\n\nProject context:\n{context}"

    try:
        reply = await router.ask(
            selection,
            [Message(role="system", content=prompt), Message(role="user", content=user)],
            role="classifier",
        )
        payload = extract_json(reply.message.content)
    except Exception:  # noqa: BLE001 - classification must never block the turn
        return Classification(Complexity.SIMPLE, score=3, reason="classifier unavailable")

    try:
        score = max(1, min(int(payload.get("score", 3)), 10))
    except (TypeError, ValueError):
        score = 3

    return Classification(
        complexity=score_to_complexity(score),
        score=score,
        reason=str(payload.get("reason") or ""),
        questions=parse_questions(payload.get("questions")),
    )


async def draft_plan(
    goal: str,
    router: Router,
    selection: Selection,
    *,
    findings: str = "",
    answers: str = "",
) -> Plan:
    """Draft a plan for a project-sized request.

    Raises:
      ValueError: If the planner returned nothing usable.
    """
    user = f"Goal:\n{goal}"
    if answers:
        user += f"\n\nThe user already answered these:\n{answers}"
    if findings:
        user += f"\n\nWhat investigation found:\n{findings}"

    reply = await router.ask(
        selection,
        [
            Message(role="system", content=PLANNER_PROMPT % {"max_steps": MAX_PLAN_STEPS}),
            Message(role="user", content=user),
        ],
        role="planner",
    )
    return parse_plan(extract_json(reply.message.content), goal)


async def revise_plan(
    plan: Plan, feedback: str, router: Router, selection: Selection
) -> Plan:
    """Produce a new revision of a plan from the user's feedback."""
    reply = await router.ask(
        selection,
        [
            Message(
                role="system",
                content=REVISION_PROMPT + "\n\n" + PLANNER_PROMPT % {"max_steps": MAX_PLAN_STEPS},
            ),
            Message(
                role="user",
                content=f"Current plan:\n\n{plan.render(chinese=False)}\n\nFeedback:\n{feedback}",
            ),
        ],
        role="planner",
    )
    revised = parse_plan(extract_json(reply.message.content), plan.goal)
    revised.revision = plan.revision + 1
    return revised
