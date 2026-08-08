"""Tests for Claude Code multi-account profiles."""

from __future__ import annotations

import json
from pathlib import Path

from aiharness.credentials import CredentialStore
from aiharness.gui.claude_profiles import ClaudeProfileStore, TEMPLATES


def test_seed_default_anthropic(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    assert store.get("anthropic") is not None
    assert store.get("login") is not None
    assert store.get("login").auth_mode == "login"
    assert store.active_id == "anthropic"


def test_upsert_key_and_launch_env(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    profile = store.upsert(
        profile_id="work",
        name="Work",
        template="anthropic",
        api_key="sk-ant-test-key-1234567890",
        model="claude-sonnet-4-5",
        make_active=True,
    )
    assert store.active_id == "work"
    env = store.launch_env(profile)
    assert env["ANTHROPIC_API_KEY"].startswith("sk-ant")
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"
    assert "CLAUDE_CONFIG_DIR" in env
    assert Path(env["CLAUDE_CONFIG_DIR"]).is_dir()
    assert store.launch_args(profile) == ["--model", "claude-sonnet-4-5"]


def test_templates_exist():
    assert "anthropic" in TEMPLATES
    assert "login" in TEMPLATES
    assert "kimi" in TEMPLATES
    assert "deepseek" in TEMPLATES
    assert "kimi-platform" in TEMPLATES
    assert "custom" in TEMPLATES
    assert TEMPLATES["login"]["auth_mode"] == "login"
    assert TEMPLATES["kimi"]["base_url"].rstrip("/").endswith("/coding")
    assert TEMPLATES["kimi"]["model"] == "k3"
    assert TEMPLATES["deepseek"]["base_url"].endswith("/anthropic")


def test_kimi_coding_launch_env_sets_anthropic_model(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    profile = store.upsert(
        profile_id="kimi-coding",
        name="Kimi Coding",
        template="kimi",
        base_url="https://api.kimi.com/coding",
        model="k3",
        api_key="sk-coding-test-key",
    )
    env = store.launch_env(profile, selected_effort="high")
    assert env["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert env["ANTHROPIC_MODEL"] == "k3"
    assert env["ANTHROPIC_API_KEY"].startswith("sk-coding")
    # Coding rejects Bearer AUTH_TOKEN; only API_KEY (x-api-key) is valid.
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "k3"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "k3"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1048576"
    assert store.launch_args(profile) == ["--model", "k3"]
    cfg = store.config_dir_for(profile)
    data = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
    assert data.get("hasCompletedOnboarding") is True
    assert data.get("penguinModeOrgEnabled") is True


def test_repair_profiles_deepseek_and_kimi_coding(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    store.upsert(
        profile_id="claude-DeepSeek",
        name="DeepSeek",
        template="custom",
        base_url="https://api.deepseek.com/v1",
        model="claude-opus-5[1m]",
        api_key="sk-ds",
    )
    store.upsert(
        profile_id="claude-Kimi",
        name="Kimi",
        template="kimi",
        base_url="https://api.kimi.com/coding/v1",
        model="kimi-k3[1m]",
        api_key="sk-k",
    )
    store.upsert(
        profile_id="claude-Kimi-moonshot-wrong",
        name="Kimi wrong url",
        template="kimi",
        base_url="https://api.moonshot.cn/anthropic",
        model="k3[1m]",
        api_key="sk-k2",
    )
    fixed = store.repair_profiles()
    assert fixed >= 1
    ds = store.get("claude-DeepSeek")
    assert ds is not None
    assert ds.base_url == "https://api.deepseek.com/anthropic"
    assert ds.template == "deepseek"
    assert not ds.model.startswith("claude")
    kimi = store.get("claude-Kimi")
    assert kimi is not None
    assert kimi.base_url.rstrip("/") == "https://api.kimi.com/coding"
    assert kimi.model == "k3"
    wrong = store.get("claude-Kimi-moonshot-wrong")
    assert wrong is not None
    assert wrong.base_url.rstrip("/") == "https://api.kimi.com/coding"
    assert wrong.model == "k3"


def test_claude_profile_proxy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://system-proxy:1")
    store = ClaudeProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    profile = store.upsert(
        profile_id="via-clash",
        name="Via Clash",
        template="anthropic",
        proxy="127.0.0.1:7897",
        api_key="sk-ant-test-key-1234567890",
    )
    env = store.launch_env(profile)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7897"
    direct = store.upsert(
        profile_id="direct",
        name="Direct",
        template="anthropic",
        proxy="直连",
    )
    env2 = store.launch_env(direct)
    assert "HTTP_PROXY" not in env2


def test_delete_profile(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    store.upsert(
        profile_id="extra",
        name="Extra",
        template="custom",
        base_url="https://proxy.example/v1",
        make_active=True,
    )
    assert store.delete("extra")
    assert store.get("extra") is None
