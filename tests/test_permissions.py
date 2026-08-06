"""Permission engine rules and danger classification."""

from __future__ import annotations

import pytest

from aiharness.config.schema import PermissionConfig
from aiharness.permissions import Decision, PermissionEngine, classify_command


def engine(workspace, **kwargs) -> PermissionEngine:
    return PermissionEngine(PermissionConfig(**kwargs), workspace)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf ~",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
        "shutdown -h now",
    ],
)
def test_catastrophic_commands_are_refused_in_every_mode(workspace, command):
    for mode in ("ask", "auto", "yolo"):
        verdict = engine(workspace, mode=mode).check("Bash", {"command": command})
        assert verdict.decision is Decision.DENY, f"{command} allowed in {mode}"


def test_catastrophic_screening_sees_through_command_chaining(workspace):
    verdict = engine(workspace, mode="yolo").check(
        "Bash", {"command": "echo hi && rm -rf / --no-preserve-root"}
    )
    assert verdict.decision is Decision.DENY


def test_auto_mode_allows_plain_commands_but_asks_about_risky_ones(workspace):
    auto = engine(workspace, mode="auto")
    assert auto.check("Bash", {"command": "ls -la"}).decision is Decision.ALLOW
    assert auto.check("Bash", {"command": "git push origin main"}).decision is Decision.ASK
    assert auto.check("Bash", {"command": "curl https://example.com"}).decision is Decision.ASK


def test_ask_mode_lets_reads_through(workspace):
    ask = engine(workspace, mode="ask")
    assert ask.check("Read", {"file_path": "a.txt"}).decision is Decision.ALLOW
    assert ask.check("Write", {"file_path": str(workspace / "a.txt")}).decision is Decision.ASK


def test_allow_rules_match_prefixes_and_globs(workspace):
    permissions = engine(
        workspace,
        mode="ask",
        allow=["Bash(git diff:*)", "Bash(npm run *)"],
    )
    assert permissions.check("Bash", {"command": "git diff --stat"}).decision is Decision.ALLOW
    assert permissions.check("Bash", {"command": "npm run build"}).decision is Decision.ALLOW
    assert permissions.check("Bash", {"command": "git commit"}).decision is Decision.ASK


def test_deny_rules_beat_allow_rules(workspace):
    permissions = engine(
        workspace, mode="yolo", allow=["Bash(*)"], deny=["Bash(sudo:*)"]
    )
    assert permissions.check("Bash", {"command": "sudo rm x"}).decision is Decision.DENY


def test_writes_outside_the_workspace_prompt(workspace, tmp_path):
    outside = tmp_path.parent / "elsewhere.txt"
    permissions = engine(workspace, mode="auto")
    verdict = permissions.check("Write", {"file_path": str(outside)})
    assert verdict.decision is Decision.ASK
    assert "outside the workspace" in verdict.reason


def test_additional_directories_extend_the_boundary(workspace, tmp_path):
    extra = tmp_path.parent / "extra"
    extra.mkdir(exist_ok=True)
    permissions = engine(
        workspace, mode="auto", additional_directories=[str(extra)]
    )
    assert permissions.check("Write", {"file_path": str(extra / "x.txt")}).decision is Decision.ALLOW


def test_session_allow_rules_take_effect_immediately(workspace):
    permissions = engine(workspace, mode="ask")
    assert permissions.check("Bash", {"command": "pytest -q"}).decision is Decision.ASK
    permissions.allow_for_session("Bash(pytest:*)")
    assert permissions.check("Bash", {"command": "pytest -q"}).decision is Decision.ALLOW


def test_ask_rules_force_prompt_even_in_yolo(workspace):
    permissions = engine(workspace, mode="yolo", ask=["Bash(npm publish:*)"])
    assert permissions.check("Bash", {"command": "npm publish"}).decision is Decision.ASK
    assert permissions.check("Bash", {"command": "ls"}).decision is Decision.ALLOW


def test_allow_persistently_updates_config_allow_list(workspace):
    permissions = engine(workspace, mode="ask")
    assert permissions.allow_persistently("Bash(pytest:*)")
    assert "Bash(pytest:*)" in permissions.cfg.allow
    assert permissions.check("Bash", {"command": "pytest -q"}).decision is Decision.ALLOW


def test_classify_command_grades_severity():
    assert classify_command("ls")[0] == "safe"
    assert classify_command("git push --force")[0] == "sensitive"
    assert classify_command("rm -rf /")[0] == "catastrophic"


def test_glob_tool_names_address_a_whole_family(workspace):
    """`mcp__github__*` must cover every tool from one MCP server."""
    permissions = engine(workspace, mode="yolo", deny=["mcp__github__*"])
    assert permissions.check("mcp__github__create_issue", {}).decision is Decision.DENY
    assert permissions.check("mcp__github__list_repos", {}).decision is Decision.DENY
    assert permissions.check("mcp__filesystem__read", {}).decision is Decision.ALLOW


def test_glob_tool_names_work_in_allow_rules(workspace):
    permissions = engine(workspace, mode="ask", allow=["mcp__*"])
    assert permissions.check("mcp__anything__at_all", {}).decision is Decision.ALLOW
    assert permissions.check("Write", {"file_path": "a.txt"}).decision is Decision.ASK


def test_plan_mode_allows_looking_at_web_pages(tmp_path):
    """Investigation is the point of plan mode, not a loophole in it.

    Blocking the browser produced plans written from memory: the agent
    reported it could not reach the network and then estimated the numbers
    it had been asked to look up.
    """
    from aiharness.config.schema import PermissionConfig
    from aiharness.permissions import Decision, PermissionEngine

    engine = PermissionEngine(PermissionConfig(mode="ask"), tmp_path)
    engine.set_plan_mode(True)

    for tool in ("BrowserNavigate", "BrowserSnapshot", "BrowserScreenshot"):
        verdict = engine.check(tool, "https://example.com")
        assert verdict.decision is not Decision.DENY, f"{tool} must survive plan mode"


def test_plan_mode_still_blocks_acting_on_a_page(tmp_path):
    """Reading someone's page is not the same as clicking their buttons."""
    from aiharness.config.schema import PermissionConfig
    from aiharness.permissions import Decision, PermissionEngine

    engine = PermissionEngine(PermissionConfig(mode="yolo"), tmp_path)
    engine.set_plan_mode(True)

    for tool in ("BrowserClick", "BrowserFill", "Write", "Edit"):
        assert engine.check(tool, "x").decision is Decision.DENY
