"""Complexity classification, clarifying questions and plan mode."""

from __future__ import annotations

import json

import pytest

from aiharness.agent.planning import (
    Complexity,
    Plan,
    PlanStep,
    build_classifier_context,
    classify_request,
    draft_plan,
    extract_json,
    parse_plan,
    parse_questions,
    score_to_complexity,
)
from aiharness.providers.base import Message, ToolCall
from aiharness.agent.prompts import build_system_prompt
from aiharness.permissions import Decision, PermissionEngine
from aiharness.providers.router import Router, Selection
from aiharness.tools.base import ToolContext
from aiharness.tools.interaction import AskUserTool, PresentPlanTool
from aiharness.toolset import build_registry

from .fake_openai import Reply

# -- parsing ---------------------------------------------------------------


def test_extract_json_survives_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here at all")


@pytest.mark.parametrize(
    "score, expected",
    [
        (1, Complexity.TRIVIAL),
        (2, Complexity.TRIVIAL),
        (3, Complexity.SIMPLE),
        (4, Complexity.SIMPLE),
        # 5-7 is the prompt's "real work" band, not its "a project" band.
        # Treating it as a project forced plan approval onto ordinary tasks,
        # and even onto questions, before any work could start.
        (5, Complexity.SIMPLE),
        (7, Complexity.SIMPLE),
        (8, Complexity.PROJECT),
        (10, Complexity.PROJECT),
    ],
)
def test_score_bands(score, expected):
    assert score_to_complexity(score) is expected


def test_the_bands_match_the_prompt_the_model_is_given():
    """The threshold and the prompt must not drift apart.

    The model is told 8-10 means "a project"; if the code disagrees, the
    model's honest answer gets reinterpreted into a different verdict.
    """
    from aiharness.agent.planning import CLASSIFIER_PROMPT
    from aiharness.constants import PROJECT_COMPLEXITY_THRESHOLD

    assert f"{PROJECT_COMPLEXITY_THRESHOLD}-10" in CLASSIFIER_PROMPT


def test_questions_need_at_least_two_real_options():
    raw = [
        {"question": "Which?", "header": "Pick", "options": [{"label": "only one"}]},
        {"question": "No options?", "header": "Pick"},
        {
            "question": "Which database?",
            "header": "Database",
            "options": [
                {"label": "Postgres", "description": "relational"},
                {"label": "SQLite", "description": "embedded"},
            ],
        },
    ]
    questions = parse_questions(raw)
    assert len(questions) == 1
    assert questions[0].header == "Database"
    assert questions[0].option_labels() == ["Postgres", "SQLite"]


def test_question_options_are_capped():
    raw = [
        {
            "question": "Which?",
            "header": "Pick",
            "options": [{"label": f"o{i}", "description": "d"} for i in range(9)],
        }
    ]
    assert len(parse_questions(raw)[0].options) == 4


def test_plan_parsing_drops_untitled_steps():
    plan = parse_plan(
        {
            "goal": "Ship the thing",
            "steps": [
                {"title": "Do it", "files": ["a.py"]},
                {"detail": "no title so it is unusable"},
            ],
            "risks": ["might break auth", "  "],
            "out_of_scope": ["the CLI"],
        },
        "fallback",
    )
    assert plan.goal == "Ship the thing"
    assert [s.title for s in plan.steps] == ["Do it"]
    assert plan.risks == ["might break auth"]
    assert plan.out_of_scope == ["the CLI"]


def test_plan_renders_scope_boundaries():
    plan = Plan(
        goal="Add caching",
        steps=[PlanStep(title="Add a cache layer", files=["cache.py"])],
        out_of_scope=["rewriting the ORM"],
    )
    rendered = plan.render(chinese=False)
    assert "Add caching" in rendered
    assert "cache.py" in rendered
    assert "rewriting the ORM" in rendered
    assert "revision 1" in rendered


# -- classification against a live (fake) model ----------------------------


async def test_classification_reads_the_models_verdict(fake, config):
    fake.push(Reply(text=json.dumps({"score": 8, "reason": "new subsystem"})))
    router = Router(config)
    try:
        verdict = await classify_request("rewrite the auth layer", router, Selection(model_id="fake"))
        assert verdict.complexity is Complexity.PROJECT
        assert verdict.needs_plan
        assert verdict.reason == "new subsystem"
    finally:
        await router.aclose()


def test_classifier_context_keeps_recent_thread_and_strips_harness_noise():
    """Follow-ups must see prior turns; clarifications/plan tags must not."""
    messages = [
        Message(role="user", content="注册商标要怎么做？", meta={"user_text": "注册商标要怎么做？"}),
        Message(role="assistant", content="先确认商品分类，再填申请表。"),
        Message(
            role="user",
            content="这里选哪个？",
            meta={
                "user_text": (
                    "这里选哪个？\n\n<clarifications>\n- 分类体系: 商标注册\n"
                    "</clarifications>\n\n[Plan mode is active. Writes are blocked.]"
                )
            },
        ),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="1", name="Read", arguments="{}")],
        ),
    ]
    ctx = build_classifier_context(messages)
    assert "注册商标要怎么做" in ctx
    assert "先确认商品分类" in ctx
    assert "这里选哪个" in ctx
    assert "clarifications" not in ctx
    assert "Plan mode is active" not in ctx
    assert "used tools: Read" in ctx


async def test_classify_request_sends_conversation_context(fake, config):
    fake.push(Reply(text=json.dumps({"score": 2, "reason": "续聊选类"})))
    router = Router(config)
    try:
        await classify_request(
            "还有微信表情包、AI短剧",
            router,
            Selection(model_id="fake"),
            context="user: 我们主要是做游戏的、AI软件要选什么类？\nassistant: 游戏软件常见…",
        )
        sent = fake.requests[-1].body
        body = sent["messages"][-1]["content"]
        assert "Conversation so far" in body
        assert "AI软件要选什么类" in body
        assert "New request" in body
        assert "微信表情包" in body
        system = sent["messages"][0]["content"]
        assert "ongoing thread" in system
    finally:
        await router.aclose()


async def test_trivial_requests_get_no_ceremony(fake, config):
    fake.push(Reply(text=json.dumps({"score": 1, "reason": "one-line answer"})))
    router = Router(config)
    try:
        verdict = await classify_request("what does this function return?", router, Selection(model_id="fake"))
        assert verdict.complexity is Complexity.TRIVIAL
        assert not verdict.needs_plan
        assert not verdict.needs_clarification
    finally:
        await router.aclose()


async def test_a_broken_classifier_falls_back_to_simple(fake, config):
    fake.push(Reply(text="I am afraid I cannot do that"))
    router = Router(config)
    try:
        verdict = await classify_request("do a thing", router, Selection(model_id="fake"))
        assert verdict.complexity is Complexity.SIMPLE
        assert "unavailable" in verdict.reason
    finally:
        await router.aclose()


async def test_draft_plan_builds_from_model_output(fake, config):
    fake.push(
        Reply(
            text=json.dumps(
                {
                    "goal": "Add rate limiting",
                    "steps": [{"title": "Add a token bucket", "files": ["limits.py"]}],
                    "risks": ["could throttle internal callers"],
                }
            )
        )
    )
    router = Router(config)
    try:
        plan = await draft_plan("add rate limiting", router, Selection(model_id="fake"))
        assert plan.goal == "Add rate limiting"
        assert plan.steps[0].files == ["limits.py"]
    finally:
        await router.aclose()


# -- plan mode blocks writes ----------------------------------------------


def test_plan_mode_blocks_writes_even_in_yolo(workspace, config):
    permissions = PermissionEngine(config.permissions, workspace)
    permissions.set_mode("yolo")
    permissions.set_plan_mode(True)

    assert permissions.check("Write", {"file_path": "a.txt"}).decision is Decision.DENY
    assert permissions.check("Edit", {"file_path": "a.txt"}).decision is Decision.DENY
    verdict = permissions.check("Write", {"file_path": "a.txt"})
    assert "plan mode" in verdict.reason


def test_plan_mode_still_allows_investigation(workspace, config):
    permissions = PermissionEngine(config.permissions, workspace)
    permissions.set_plan_mode(True)
    for tool, args in (
        ("Read", {"file_path": "a.txt"}),
        ("Grep", {"pattern": "x"}),
        ("Glob", {"pattern": "*.py"}),
        ("TodoWrite", {}),
        ("AskUser", {}),
        ("PresentPlan", {}),
    ):
        assert permissions.check(tool, args).decision is not Decision.DENY, tool


@pytest.mark.parametrize(
    "command, allowed",
    [
        ("ls -la", True),
        ("git status", True),
        ("git log --oneline", True),
        ("cat setup.py", True),
        ("python --version", True),
        ("git push", False),
        ("npm install", False),
        ("rm file.txt", False),
        ("echo hi > out.txt && rm x", False),
    ],
)
def test_plan_mode_only_allows_inspection_commands(workspace, config, command, allowed):
    permissions = PermissionEngine(config.permissions, workspace)
    permissions.set_mode("yolo")
    permissions.set_plan_mode(True)
    verdict = permissions.check("Bash", {"command": command})
    assert (verdict.decision is not Decision.DENY) is allowed, command


def test_leaving_plan_mode_restores_normal_rules(workspace, config):
    permissions = PermissionEngine(config.permissions, workspace)
    permissions.set_mode("yolo")
    permissions.set_plan_mode(True)
    assert permissions.check("Write", {"file_path": "a.txt"}).decision is Decision.DENY
    permissions.set_plan_mode(False)
    assert permissions.check("Write", {"file_path": "a.txt"}).decision is Decision.ALLOW


def test_plan_mode_changes_the_system_prompt(workspace):
    normal = build_system_prompt(workspace, permission_mode="yolo")
    planning = build_system_prompt(workspace, permission_mode="yolo", plan_mode=True)
    assert "Plan mode is active" in planning
    assert "Plan mode is active" not in normal


# -- the interaction tools -------------------------------------------------


def make_ctx(config, workspace, router, **kwargs) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        config=config,
        permissions=PermissionEngine(config.permissions, workspace),
        router=router,
        **kwargs,
    )


async def test_ask_user_returns_the_users_answers(config, workspace, router):
    captured: list = []

    async def fake_ask(questions):
        captured.extend(questions)
        return {"Database": "Postgres"}

    ctx = make_ctx(config, workspace, router, ask_user=fake_ask)
    result = await AskUserTool().run(
        {
            "questions": [
                {
                    "question": "Which database?",
                    "header": "Database",
                    "options": [
                        {"label": "Postgres", "description": "relational"},
                        {"label": "SQLite", "description": "embedded"},
                    ],
                }
            ]
        },
        ctx,
    )
    assert not result.is_error
    assert "Postgres" in result.content
    assert len(captured) == 1
    await router.aclose()


async def test_ask_user_headless_tells_the_model_to_decide(config, workspace, router):
    ctx = make_ctx(config, workspace, router)  # no ask_user callback
    result = await AskUserTool().run(
        {
            "questions": [
                {
                    "question": "Which?",
                    "header": "Pick",
                    "options": [
                        {"label": "A", "description": "a"},
                        {"label": "B", "description": "b"},
                    ],
                }
            ]
        },
        ctx,
    )
    assert result.is_error
    assert "Choose the most reasonable option yourself" in result.content
    await router.aclose()


async def test_present_plan_reports_approval(config, workspace, router):
    async def approve(plan):
        return True, ""

    ctx = make_ctx(config, workspace, router, present_plan=approve)
    result = await PresentPlanTool().run(
        {"goal": "Ship it", "steps": [{"title": "Write the code", "files": ["a.py"]}]}, ctx
    )
    assert not result.is_error
    assert "approved" in result.content
    assert ctx.plan.goal == "Ship it"
    await router.aclose()


async def test_present_plan_relays_feedback_and_blocks(config, workspace, router):
    async def reject(plan):
        return False, "use a queue instead of polling"

    ctx = make_ctx(config, workspace, router, present_plan=reject)
    result = await PresentPlanTool().run(
        {"goal": "Poll the API", "steps": [{"title": "Add a poller"}]}, ctx
    )
    assert "use a queue instead of polling" in result.content
    assert "Do not start writing files" in result.content
    await router.aclose()


async def test_present_plan_rejects_an_empty_plan(config, workspace, router):
    ctx = make_ctx(config, workspace, router)
    result = await PresentPlanTool().run({"goal": "x", "steps": []}, ctx)
    assert result.is_error
    await router.aclose()


def test_interaction_tools_are_not_given_to_subagents():
    registry = build_registry()
    names = {spec["function"]["name"] for spec in registry.specs(subagent=True)}
    assert "AskUser" not in names
    assert "PresentPlan" not in names
    assert "Read" in names


# -- routing inside the app ------------------------------------------------


async def test_a_project_request_puts_the_app_into_plan_mode(config, workspace, sessions, fake):
    from aiharness.ui.app import HarnessApp

    config.planning.auto_classify = True
    app = HarnessApp(config, workspace)
    fake.push(
        Reply(text=json.dumps({"score": 9, "reason": "new subsystem"})),
        Reply(text="Let me look around first."),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "rewrite the whole billing system"
        await pilot.press("enter")
        await pilot.pause(delay=1.5)

        assert app.plan_mode is True
        assert app.permissions.plan_mode is True


async def test_a_trivial_request_does_not_enter_plan_mode(config, workspace, sessions, fake):
    from aiharness.ui.app import HarnessApp

    config.planning.auto_classify = True
    app = HarnessApp(config, workspace)
    fake.push(
        Reply(text=json.dumps({"score": 1, "reason": "a lookup"})),
        Reply(text="It returns an int."),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "what does parse() return?"
        await pilot.press("enter")
        await pilot.pause(delay=1.5)

        assert app.plan_mode is False


async def test_clarifying_answers_are_appended_to_the_prompt(config, workspace, sessions, fake):
    from aiharness.ui.app import HarnessApp

    config.planning.auto_classify = True
    app = HarnessApp(config, workspace)

    async def answer(questions):
        return {"Database": "Postgres"}

    fake.push(
        Reply(
            text=json.dumps(
                {
                    "score": 3,
                    "reason": "needs a choice",
                    "questions": [
                        {
                            "question": "Which database?",
                            "header": "Database",
                            "options": [
                                {"label": "Postgres", "description": "relational"},
                                {"label": "SQLite", "description": "embedded"},
                            ],
                        }
                    ],
                }
            )
        ),
        Reply(text="Using Postgres then."),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ask_user = answer  # bypass the modal
        app.query_one("#prompt").value = "add persistence"
        await pilot.press("enter")
        await pilot.pause(delay=1.5)

        first_user = next(m for m in app.agent.messages if m.role == "user")
        assert "<clarifications>" in first_user.content
        assert "Postgres" in first_user.content


async def test_plan_command_blocks_writes(config, workspace, sessions):
    from aiharness.ui.app import HarnessApp
    from aiharness.ui.commands import dispatch

    app = HarnessApp(config, workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(app, "/plan")
        assert "Plan mode" in output
        assert app.permissions.check("Write", {"file_path": "a.txt"}).decision is Decision.DENY

        await dispatch(app, "/plan off")
        assert app.permissions.check("Write", {"file_path": "a.txt"}).decision is Decision.ALLOW
