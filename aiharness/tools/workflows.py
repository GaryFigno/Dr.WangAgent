"""Adversarial review and verification workflows."""

from __future__ import annotations

from typing import Any

from ..agent.subagent import SubagentSpec, run_parallel, run_subagent
from ..constants import (
    MAX_ADVERSARIAL_ROUNDS,
    REVIEW_MAX_TURNS,
    VERIFY_CHECK_OUTPUT_CHARS,
    VERIFY_COMMAND_TIMEOUT,
)
from ..providers.router import NoRouteError, Selection
from .base import Tool, ToolContext, ToolResult

#: Reviewers and verifiers never write; they read and run checks.
REVIEW_TOOLS = ["Read", "Glob", "Grep", "Bash"]

ADVERSARY_PROMPT = """\
You are a hostile reviewer. Your job is to find what is wrong with the work \
described below — not to praise it, not to summarise it, not to suggest \
stylistic preferences.

Read the actual code with the tools available. Do not review from the \
description alone; a claim you have not checked against the source is worthless.

Hunt specifically for:

- Correctness bugs: wrong logic, off-by-one, inverted conditions, bad operator \
precedence, mishandled empty/null/zero cases.
- Broken contracts: a caller that will now receive a different shape, a changed \
signature with unupdated call sites, a swallowed exception.
- Concurrency and ordering: races, unawaited coroutines, shared mutable state, \
resources never released.
- Security: injection, path traversal, secrets in logs, missing authorisation, \
unsafe deserialisation.
- Failure modes the author clearly did not consider: what happens on a network \
error, a partial write, a very large input, a hostile input.

For each finding, give exactly this:

  FINDING: <one sentence stating the defect>
  WHERE: <path:line>
  TRIGGER: <concrete inputs or state that produce the failure>
  IMPACT: <what actually goes wrong for a user>

Rank findings by severity, worst first. If you genuinely find nothing after \
reading the code, say "NO BLOCKING ISSUES" and name the three riskiest \
assumptions the work depends on. Never pad the list to look thorough — a \
fabricated finding is worse than no finding.
"""

TRIAGE_PROMPT = """\
An adversarial reviewer produced the findings below. Some are real, some are \
misreadings, some are pedantry. Check each one against the actual code with \
your tools and rule on it.

For each finding output:

  <original one-line summary>
  VERDICT: CONFIRMED | PLAUSIBLE | WRONG | OUT-OF-SCOPE
  EVIDENCE: <the specific code at path:line that settles it>

CONFIRMED means you traced the failure path yourself and it holds. PLAUSIBLE \
means the code is genuinely ambiguous and it depends on an invariant you cannot \
check. WRONG means the reviewer misread the code — quote the line proving it. \
OUT-OF-SCOPE means real but unrelated to the work under review.

Then list the CONFIRMED findings in severity order as the actionable set. Be \
willing to say every finding was wrong; that is a valid outcome.
"""

VERIFY_PROMPT = """\
You are checking whether completed work actually does what was asked. You are \
not the author and you owe them nothing.

Compare the requirement against the real state of the code, reading it with \
your tools. Determine, concretely:

1. Does it do everything the requirement asked, or only part? Name each \
requirement and mark it done / partial / missing.
2. Does it do things the requirement did not ask for? Name them.
3. Do the automated checks pass? The output is supplied below — read it rather \
than assuming.
4. Are there obvious cases the implementation gets wrong?

Finish with a single line:

  RESULT: PASS | PASS-WITH-ISSUES | FAIL

PASS means every requirement is met and the checks are green. Do not award PASS \
because the work looks like a reasonable effort.
"""


def _selection_for(ctx: ToolContext, spec: str | None, role: str) -> Selection:
    if spec:
        return Selection.parse(spec, ctx.config)
    binding = ctx.config.role(role)
    if not binding:
        raise NoRouteError(f"role '{role}' is not configured")
    return Selection.from_binding(binding)


class ChallengeTool(Tool):
    name = "Challenge"
    subagent_safe = False
    bulky = True
    description = """
Have an adversarial model attack your work, then triage its findings.

Round 1: one or more adversary models read the code and try to break it.
Round 2: a separate model checks each finding against the source and rules it
CONFIRMED / PLAUSIBLE / WRONG, so you do not act on hallucinated bugs.

Use before committing to a design, on anything touching security, money,
data loss or auth, and whenever you are confident without much evidence.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "What to attack: the change, design or claim, described concretely",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths the reviewer should read first",
                },
                "rounds": {
                    "type": "integer",
                    "description": "How many independent adversaries to run (default from config)",
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit adversary models, e.g. ['kimi@openrouter']",
                },
                "triage": {"type": "boolean", "description": "Run the triage pass (default true)"},
            },
            "required": ["target"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry

        target = str(args.get("target", "")).strip()
        if not target:
            return ToolResult.error("target is empty")

        acfg = ctx.config.workflows.adversarial
        rounds = int(args.get("rounds") or acfg.rounds or 1)
        rounds = max(1, min(rounds, MAX_ADVERSARIAL_ROUNDS))

        try:
            if args.get("models"):
                selections = [Selection.parse(str(m), ctx.config) for m in args["models"]]
            else:
                selections = [_selection_for(ctx, None, acfg.adversary_role)]
        except NoRouteError as e:
            return ToolResult.error(str(e))

        files = [str(f) for f in (args.get("files") or [])]
        file_note = ""
        if files:
            file_note = "\n\nStart by reading these files:\n" + "\n".join(f"- {f}" for f in files)

        registry = build_registry(include_agent_tools=False)
        specs = [
            SubagentSpec(
                prompt=f"Work under review:\n\n{target}{file_note}",
                selection=selections[i % len(selections)],
                label=f"adversary-{i + 1}",
                system_prompt=_adversary_system(ctx),
                max_turns=REVIEW_MAX_TURNS,
                tool_names=list(REVIEW_TOOLS),
            )
            for i in range(rounds)
        ]

        ctx.note(f"adversarial review: {rounds} pass(es) on " + ", ".join(s.selection.label() for s in specs))
        results = await run_parallel(
            specs, ctx, registry, limit=rounds,
            on_progress=lambda label, line: ctx.note(f"[{label}] {line}"),
        )

        critiques = [r for r in results if r.text.strip()]
        if not critiques:
            errs = "; ".join(r.error for r in results if r.error)
            return ToolResult.error(f"adversarial review produced nothing ({errs})")

        combined = "\n\n".join(
            f"### {r.label} ({r.selection})\n\n{r.text}" for r in critiques
        )
        cost = sum(r.cost for r in results)

        clean = all("NO BLOCKING ISSUES" in r.text.upper() for r in critiques)
        if clean and acfg.stop_when_clean:
            return ToolResult(
                content=f"## Adversarial review — no blocking issues\n\n{combined}",
                summary=f"challenge clean — ${cost:.4f}",
            )

        if args.get("triage") is False:
            return ToolResult(
                content=f"## Adversarial review\n\n{combined}",
                summary=f"{len(critiques)} critique(s) — ${cost:.4f}",
            )

        ctx.note("triaging findings")
        try:
            triage_selection = _selection_for(ctx, None, ctx.config.workflows.verify.verifier_role)
        except NoRouteError as e:
            return ToolResult(
                content=f"## Adversarial review\n\n{combined}\n\n_(triage skipped: {e})_",
                summary=f"{len(critiques)} critique(s), untriaged",
            )

        triage = await run_subagent(
            SubagentSpec(
                prompt=(
                    f"Work under review:\n\n{target}{file_note}\n\n"
                    f"Findings to rule on:\n\n{combined}"
                ),
                selection=triage_selection,
                label="triage",
                system_prompt=_prompt_with(ctx, TRIAGE_PROMPT),
                max_turns=REVIEW_MAX_TURNS,
                tool_names=list(REVIEW_TOOLS),
            ),
            ctx,
            registry,
            on_progress=lambda label, line: ctx.note(f"[{label}] {line}"),
        )
        cost += triage.cost

        body = f"## Triaged findings\n\n{triage.text or '(triage produced nothing)'}"
        body += f"\n\n---\n\n<details>\n## Raw critiques\n\n{combined}\n</details>"
        return ToolResult(
            content=body,
            summary=f"challenge: {len(critiques)} critique(s) triaged — ${cost:.4f}",
            display={"kind": "challenge", "rounds": rounds},
        )


class VerifyTool(Tool):
    name = "Verify"
    subagent_safe = False
    bulky = True
    description = """
Run the project's checks and have a reviewer model confirm the work matches
the request.

Executes the configured verification commands (tests, lint, typecheck) — or
the ones you pass — then a separate model reads the requirement and the code
and rules PASS / PASS-WITH-ISSUES / FAIL.

Use before telling the user something is done. Commands run through the normal
permission engine, so they may prompt.
"""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "What was asked for, in the user's terms",
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell checks to run; defaults to the configured ones",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths that changed",
                },
                "model": {"type": "string", "description": "Override the verifier model"},
            },
            "required": ["requirement"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..toolset import build_registry
        from .shell import BashTool

        vcfg = ctx.config.workflows.verify
        if not vcfg.enabled:
            return ToolResult.error("the verify workflow is disabled in config")

        requirement = str(args.get("requirement", "")).strip()
        if not requirement:
            return ToolResult.error("requirement is empty")

        commands = [str(c) for c in (args.get("commands") or vcfg.commands or [])]
        transcript: list[str] = []
        failures = 0

        bash = BashTool()
        for command in commands:
            ctx.note(f"verify: {command}")
            result = await bash.guarded_run(
                {"command": command, "timeout": VERIFY_COMMAND_TIMEOUT}, ctx
            )
            status = "FAILED" if result.is_error else "ok"
            if result.is_error:
                failures += 1
            body = result.content[:VERIFY_CHECK_OUTPUT_CHARS]
            transcript.append(f"$ {command}\n[{status}]\n{body}")

        checks = "\n\n".join(transcript) if transcript else "(no verification commands configured)"

        try:
            selection = _selection_for(ctx, args.get("model"), vcfg.verifier_role)
        except NoRouteError as e:
            return ToolResult.error(str(e))

        files = [str(f) for f in (args.get("files") or [])]
        file_note = "\n\nChanged files:\n" + "\n".join(f"- {f}" for f in files) if files else ""

        ctx.note(f"verifying with {selection.label()}")
        review = await run_subagent(
            SubagentSpec(
                prompt=(
                    f"Requirement:\n{requirement}{file_note}\n\n"
                    f"Automated check output:\n\n<checks>\n{checks}\n</checks>"
                ),
                selection=selection,
                label="verifier",
                system_prompt=_prompt_with(ctx, VERIFY_PROMPT),
                max_turns=REVIEW_MAX_TURNS,
                tool_names=list(REVIEW_TOOLS),
            ),
            ctx,
            build_registry(include_agent_tools=False),
            on_progress=lambda label, line: ctx.note(f"[{label}] {line}"),
        )

        verdict = "UNKNOWN"
        for line in reversed((review.text or "").splitlines()):
            if "RESULT:" in line.upper():
                verdict = line.split(":", 1)[1].strip()
                break

        summary = f"verify: {verdict}"
        if commands:
            summary += f" ({len(commands) - failures}/{len(commands)} checks green)"

        body = (
            f"## Verification — {verdict}\n\n"
            f"{review.text or '(reviewer produced nothing)'}\n\n"
            f"---\n\n<details>\n## Check output\n\n```\n{checks}\n```\n</details>"
        )
        return ToolResult(
            content=body,
            summary=summary + f" — ${review.cost:.4f}",
            is_error=verdict.upper().startswith("FAIL"),
            display={"kind": "verify", "verdict": verdict, "failures": failures},
        )


# --------------------------------------------------------------------------


def _prompt_with(ctx: ToolContext, extra: str) -> str:
    from ..agent.prompts import build_system_prompt
    from .shell import find_shell

    _, _, dialect = find_shell()
    return build_system_prompt(
        ctx.workspace,
        shell=dialect,
        extra=extra,
        permission_mode=ctx.permissions.mode,
    )


def _adversary_system(ctx: ToolContext) -> str:
    return _prompt_with(ctx, ADVERSARY_PROMPT)
