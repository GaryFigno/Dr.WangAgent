"""Mining past sessions for habits worth saving as skills."""

from __future__ import annotations

import json

import pytest

from aiharness.providers.base import Message, ToolCall
from aiharness.providers.router import Router, Selection
from aiharness.skills import SkillLibrary, read_skill_text
from aiharness.workflows.learning import (
    SkillCandidate,
    collect_digests,
    digest_session,
    mine_skills,
    repeated_commands,
    save_candidate,
)

from .fake_openai import Reply


def bash_call(command: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="Bash", arguments=json.dumps({"command": command}))


def build_session(sessions, workspace, request: str, commands: list[str], files=()):
    handle = sessions.create(workspace)
    handle.append(Message(role="user", content=f"<environment>\nDate: x\n</environment>\n\n{request}",
                          meta={"user_text": request}))
    calls = [bash_call(cmd, f"c{i}") for i, cmd in enumerate(commands)]
    for path in files:
        calls.append(
            ToolCall(id=f"f{path}", name="Read", arguments=json.dumps({"file_path": path}))
        )
    handle.append(Message(role="assistant", content="", tool_calls=calls))
    handle.append(Message(role="assistant", content="done"))
    return handle


# -- digesting -------------------------------------------------------------


def test_digest_strips_the_environment_block(sessions, workspace):
    handle = build_session(sessions, workspace, "run the tests", ["pytest -q"])
    digest = digest_session(handle)
    assert digest is not None
    assert digest.requests == ["run the tests"]
    assert "environment" not in digest.requests[0]
    assert digest.commands == ["pytest -q"]


def test_digest_skips_sessions_with_no_request(sessions, workspace):
    handle = sessions.create(workspace)
    handle.append(Message(role="assistant", content="hello"))
    handle.append(Message(role="assistant", content="anyone there?"))
    assert digest_session(handle) is None


def test_digest_skips_a_one_message_session(sessions, workspace):
    handle = sessions.create(workspace)
    handle.append(Message(role="user", content="hi"))
    assert digest_session(handle) is None


def test_digest_ignores_bookkeeping_tools(sessions, workspace):
    handle = sessions.create(workspace)
    handle.append(Message(role="user", content="do it", meta={"user_text": "do it"}))
    handle.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="t", name="TodoWrite", arguments="{}"),
                bash_call("make build"),
            ],
        )
    )
    digest = digest_session(handle)
    assert digest.tools == ["Bash"]


def test_digest_deduplicates_files_and_commands(sessions, workspace):
    handle = build_session(
        sessions, workspace, "look at it",
        ["pytest -q", "pytest -q"], files=["a.py", "a.py", "b.py"],
    )
    digest = digest_session(handle)
    assert digest.commands == ["pytest -q"]
    assert digest.files == ["a.py", "b.py"]


def test_collect_digests_reads_every_session(sessions, workspace):
    for index in range(3):
        build_session(sessions, workspace, f"task {index}", ["make test"])
    digests = collect_digests(sessions, workspace=workspace)
    assert len(digests) == 3


# -- repetition detection --------------------------------------------------


def test_repeated_commands_counts_sessions_not_occurrences(sessions, workspace):
    """Running pytest twelve times in one session is one habit, not twelve."""
    handle = sessions.create(workspace)
    handle.append(Message(role="user", content="go", meta={"user_text": "go"}))
    handle.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[bash_call(f"pytest tests/test_{i}.py", f"c{i}") for i in range(12)],
        )
    )
    digests = [digest_session(handle)]
    assert repeated_commands(digests, minimum=2) == {}


def test_repeated_commands_finds_a_cross_session_habit(sessions, workspace):
    for index in range(3):
        build_session(sessions, workspace, f"task {index}", ["uv run pytest -q"])
    digests = collect_digests(sessions, workspace=workspace)
    assert repeated_commands(digests, minimum=3) == {"uv run pytest": 3}


def test_repeated_commands_respects_the_threshold(sessions, workspace):
    for index in range(2):
        build_session(sessions, workspace, f"task {index}", ["make lint"])
    digests = collect_digests(sessions, workspace=workspace)
    assert repeated_commands(digests, minimum=3) == {}


# -- the miner -------------------------------------------------------------


async def test_mining_needs_enough_sessions(config, sessions, workspace):
    build_session(sessions, workspace, "one task", ["make"])
    digests = collect_digests(sessions, workspace=workspace)
    router = Router(config)
    try:
        assert await mine_skills(digests, router, Selection(model_id="fake")) == []
    finally:
        await router.aclose()


async def test_mining_returns_validated_candidates(fake, config, sessions, workspace):
    for index in range(4):
        build_session(sessions, workspace, f"task {index}", ["uv run pytest -q"])
    digests = collect_digests(sessions, workspace=workspace)

    fake.push(
        Reply(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "name": "Run Project Tests",
                            "description": "Use uv to run pytest. Use whenever tests are run.",
                            "body": "Always `uv run pytest -q`, never bare pytest.",
                            "evidence": ["seen in 4 sessions"],
                            "occurrences": 4,
                        },
                        {"name": "too-rare", "description": "d", "body": "b", "occurrences": 1},
                        {"name": "", "description": "no name", "body": "b", "occurrences": 5},
                    ]
                }
            )
        )
    )
    router = Router(config)
    try:
        candidates = await mine_skills(digests, router, Selection(model_id="fake"), minimum=3)
    finally:
        await router.aclose()

    assert len(candidates) == 1
    assert candidates[0].name == "run-project-tests"  # slugified
    assert candidates[0].occurrences == 4


async def test_a_non_json_reply_yields_nothing(fake, config, sessions, workspace):
    for index in range(4):
        build_session(sessions, workspace, f"task {index}", ["make"])
    digests = collect_digests(sessions, workspace=workspace)
    fake.push(Reply(text="I could not find any patterns, sorry!"))
    router = Router(config)
    try:
        assert await mine_skills(digests, router, Selection(model_id="fake")) == []
    finally:
        await router.aclose()


async def test_an_empty_candidate_list_is_a_valid_answer(fake, config, sessions, workspace):
    for index in range(4):
        build_session(sessions, workspace, f"task {index}", ["make"])
    digests = collect_digests(sessions, workspace=workspace)
    fake.push(Reply(text=json.dumps({"candidates": []})))
    router = Router(config)
    try:
        assert await mine_skills(digests, router, Selection(model_id="fake")) == []
    finally:
        await router.aclose()


# -- saving ----------------------------------------------------------------


def test_saved_candidate_is_a_loadable_skill(tmp_path):
    candidate = SkillCandidate(
        name="run-tests",
        description="Run the project's tests with uv. Use whenever tests are needed.",
        body="Always `uv run pytest -q`.",
        occurrences=4,
    )
    skills_dir = tmp_path / "skills"
    path = save_candidate(candidate, skills_dir)

    assert path.exists()
    library = SkillLibrary(tmp_path, extra_paths=[str(skills_dir)]).load()
    loaded = library.get("run-tests")
    assert loaded is not None
    assert "uv run pytest" in loaded.body


def test_saving_never_overwrites_an_existing_skill(tmp_path):
    candidate = SkillCandidate(name="dup", description="d", body="b", occurrences=3)
    skills_dir = tmp_path / "skills"
    save_candidate(candidate, skills_dir)
    with pytest.raises(FileExistsError):
        save_candidate(candidate, skills_dir)


def test_candidate_markdown_has_valid_frontmatter():
    candidate = SkillCandidate(
        name="x", description="line one\n  line two", body="# Body", occurrences=3
    )
    text = candidate.to_markdown()
    assert text.startswith("---\nname: x\n")
    # The description must stay on one line or the YAML breaks.
    assert "description: line one line two\n" in text


# -- encoding robustness ---------------------------------------------------


def test_skills_written_in_gbk_still_load(tmp_path):
    """A Chinese Windows editor defaults to GBK; those skills must not vanish."""
    folder = tmp_path / "skills" / "gbk-skill"
    folder.mkdir(parents=True)
    content = (
        "---\nname: gbk-skill\ndescription: 用中文写的技能描述，用于测试编码。\n---\n\n正文内容。\n"
    )
    (folder / "SKILL.md").write_bytes(content.encode("gbk"))

    assert "中文" in (read_skill_text(folder / "SKILL.md") or "")
    library = SkillLibrary(tmp_path, extra_paths=[str(tmp_path / "skills")]).load()
    skill = library.get("gbk-skill")
    assert skill is not None
    assert "技能描述" in skill.description


# -- reusing an existing Claude Code skill library --------------------------


def test_a_claude_code_skill_library_loads_unchanged(tmp_path):
    """An existing ~/.claude/skills layout must work with no migration.

    The point of matching Claude Code's layout is that a user with a mature
    skill library can point this harness at it and keep everything.
    """
    shared = tmp_path / "shared-skills"
    for name, description in (
        ("media-pipeline", "Cut out backgrounds and key video. Use for game art."),
        ("quant-loop", "Run the backtest loop. Use when backtesting a strategy."),
    ):
        folder = shared / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n"
            f"Run `python tools/{name}.py --help` first.\n",
            encoding="utf-8",
        )
        # Skills commonly ship helper files beside SKILL.md.
        (folder / "scripts").mkdir()
        (folder / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")

    library = SkillLibrary(tmp_path, extra_paths=[str(shared)]).load()

    assert {s.name for s in library.all()} == {"media-pipeline", "quant-loop"}
    assert library.errors == []
    # Bundled files are advertised so the model knows they are there.
    rendered = library.get("quant-loop").render()
    assert "run.py" in rendered


def test_project_skills_win_over_shared_ones(tmp_path):
    """A project override must beat the shared library of the same name."""
    workspace = tmp_path / "project"
    project_skill = workspace / ".aiharness" / "skills" / "build"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        "---\nname: build\ndescription: Project build. Use for this repo.\n---\n\nlocal\n",
        encoding="utf-8",
    )
    shared = tmp_path / "shared" / "build"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(
        "---\nname: build\ndescription: Generic build. Use anywhere.\n---\n\nshared\n",
        encoding="utf-8",
    )

    library = SkillLibrary(workspace, extra_paths=[str(tmp_path / "shared")]).load()
    assert library.get("build").body.strip() == "local"


def test_the_prompt_listing_stays_small_with_many_skills(tmp_path):
    """Progressive disclosure: a big library must stay cheap in context."""
    shared = tmp_path / "many"
    for index in range(40):
        folder = shared / f"skill-{index}"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: skill-{index}\ndescription: Does thing {index}. "
            f"Use when thing {index} is needed.\n---\n\n" + ("body " * 2000),
            encoding="utf-8",
        )

    library = SkillLibrary(tmp_path, extra_paths=[str(shared)]).load()
    listing = library.prompt_section()

    assert len(library.all()) == 40
    # 40 skills with 10k-character bodies must not put 400k chars in the prompt.
    assert len(listing) < 8000
    assert "body body" not in listing
