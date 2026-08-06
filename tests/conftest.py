"""Shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiharness.config.schema import (  # noqa: E402
    Config,
    ContextConfig,
    EffortSpec,
    ModelDef,
    PermissionConfig,
    PlanningConfig,
    Pricing,
    ProviderAccount,
    RoleBinding,
)
from aiharness.permissions import PermissionEngine  # noqa: E402
from aiharness.providers.router import Router  # noqa: E402
from aiharness.session.store import SessionStore  # noqa: E402
from aiharness.toolset import build_registry  # noqa: E402

from .fake_openai import FakeOpenAI  # noqa: E402

#: Small window so compaction can be triggered without huge transcripts.
TEST_CONTEXT_WINDOW = 4000


@pytest.fixture
def fake():
    with FakeOpenAI() as server:
        yield server


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "hello.txt").write_text("line one\nline two\n", encoding="utf-8")
    return tmp_path


def make_config(
    base_url: str,
    *,
    extra_accounts: list[ProviderAccount] | None = None,
    context_window: int = TEST_CONTEXT_WINDOW,
) -> Config:
    """Build a config whose single model is served by the fake endpoint."""
    accounts = [
        ProviderAccount(id="primary", base_url=base_url, api_key="key-primary", priority=10)
    ]
    accounts += extra_accounts or []
    model = ModelDef(
        id="fake",
        model="fake-model",
        accounts=[account.id for account in accounts],
        context_windows=[context_window],
        default_context=context_window,
        max_output_tokens=256,
        effort=EffortSpec(mode="reasoning_effort", levels={"low": "low", "high": "high"}),
        default_effort="low",
        pricing=Pricing(input=1.0, output=2.0, cached_input=0.1),
    )
    return Config(
        accounts=accounts,
        models=[model],
        roles={
            "main": RoleBinding(model="fake"),
            "cheap": RoleBinding(model="fake"),
            "compactor": RoleBinding(model="fake"),
            "adversary": RoleBinding(model="fake"),
            "verifier": RoleBinding(model="fake"),
            "researcher": RoleBinding(model="fake"),
        },
        permissions=PermissionConfig(mode="yolo"),
        context=ContextConfig(compact_threshold=0.8, keep_recent_messages=2),
        # Off by default in tests so a scripted reply is consumed by the turn
        # under test rather than by an invisible classification call. Tests
        # that care about routing turn it on explicitly.
        planning=PlanningConfig(auto_classify=False),
        max_agent_turns=6,
    )


@pytest.fixture
def config(fake, workspace) -> Config:
    return make_config(fake.base_url)


@pytest.fixture
def router(config) -> Router:
    return Router(config)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests away from the real user config and data directories.

    This is enforced at the source rather than by listing files. An earlier
    version named each file it knew about, and was wrong twice: once a test
    overwrote the developer's own ``config.yaml``, and once stale pytest
    directories leaked into the recent-projects list and showed up in the
    real UI. Both times the list was simply missing an entry, and both times
    the test suite passed anyway.

    So every ``platformdirs`` lookup inside the package is redirected here,
    which means a module added tomorrow is covered without anybody
    remembering to update this fixture. The environment variables are set too,
    for the code paths that consult them before calling platformdirs.
    """
    home = tmp_path / "profile"

    def fake_config_dir(*args: object, **kwargs: object) -> str:
        return str(home / "config")

    def fake_data_dir(*args: object, **kwargs: object) -> str:
        return str(home / "data")

    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("aiharness"):
            continue
        if hasattr(module, "user_config_dir"):
            monkeypatch.setattr(module, "user_config_dir", fake_config_dir)
        if hasattr(module, "user_data_dir"):
            monkeypatch.setattr(module, "user_data_dir", fake_data_dir)

    monkeypatch.setenv("AIH_PREFS_FILE", str(tmp_path / "ui.json"))
    monkeypatch.setenv("AIH_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("AIH_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("AIH_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("AIH_WORKSPACES_FILE", str(tmp_path / "workspaces.json"))
    monkeypatch.setenv("AIH_SKILL_ROOT", str(tmp_path / "skills"))
    # The same directory the ``sessions`` fixture uses, so a store built by
    # hand and one built from the environment agree about where sessions are.
    monkeypatch.setenv("AIH_SESSION_DIR", str(tmp_path / "sessions"))


@pytest.fixture
def sessions(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


@pytest.fixture
def agent_parts(config, router, workspace):
    """The pieces an Agent needs, ready to assemble."""
    permissions = PermissionEngine(config.permissions, workspace)
    return config, router, build_registry(), permissions, workspace
