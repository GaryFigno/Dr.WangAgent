"""Tests for Agent ↔ Codex/Claude credential bridging."""

from __future__ import annotations

from pathlib import Path

from aiharness.config.schema import ProviderAccount
from aiharness.credentials import CredentialStore
from aiharness.gui.claude_profiles import ClaudeProfileStore, TEMPLATES as CLAUDE_TEMPLATES
from aiharness.gui.codex_profiles import CodexProfileStore
from aiharness.gui.profile_bridge import (
    claude_base_url_for_account,
    claude_model_for_account,
    codex_model_for_account,
    detect_claude_template,
    detect_codex_template,
    is_kimi_coding,
    match_agent_secret,
    vendor_family,
)


def test_vendor_family_moonshot_paths():
    assert vendor_family("https://api.moonshot.cn/v1") == "moonshot"
    assert vendor_family("https://api.moonshot.cn/anthropic") == "moonshot"
    assert vendor_family("https://api.moonshot.ai/anthropic") == "moonshot"


def test_vendor_family_kimi_coding_and_deepseek():
    assert is_kimi_coding("https://api.kimi.com/coding/v1")
    assert vendor_family("https://api.kimi.com/coding/v1") == "kimi-coding"
    assert vendor_family("https://api.kimi.com/coding") == "kimi-coding"
    assert vendor_family("https://api.deepseek.com/v1") == "deepseek"
    assert vendor_family("https://api.deepseek.com/anthropic") == "deepseek"


def test_match_agent_secret_across_moonshot_paths():
    accounts = [
        ProviderAccount(
            id="kimi-work",
            base_url="https://api.moonshot.cn/v1",
            api_key="sk-moonshot-test-key",
            api_key_env="KIMI_API_KEY",
        )
    ]
    secret = match_agent_secret(
        base_url="https://api.moonshot.cn/anthropic",
        env_key="ANTHROPIC_API_KEY",
        accounts=accounts,
    )
    assert secret == "sk-moonshot-test-key"


def test_match_agent_secret_kimi_coding_paths():
    accounts = [
        ProviderAccount(
            id="coding",
            base_url="https://api.kimi.com/coding/v1",
            api_key="sk-coding-key",
        )
    ]
    secret = match_agent_secret(
        base_url="https://api.kimi.com/coding",
        env_key="ANTHROPIC_API_KEY",
        accounts=accounts,
    )
    assert secret == "sk-coding-key"


def test_detect_templates():
    account = ProviderAccount(id="k", base_url="https://api.moonshot.cn/v1", api_key="x")
    assert detect_codex_template(account) == "kimi"
    assert detect_claude_template(account) == "kimi-platform"
    assert claude_base_url_for_account(account) == "https://api.moonshot.cn/anthropic"


def test_claude_url_mapping_kimi_coding_and_deepseek():
    coding = ProviderAccount(id="c", base_url="https://api.kimi.com/coding/v1", api_key="x")
    assert detect_claude_template(coding) == "kimi"
    assert claude_base_url_for_account(coding).rstrip("/") == "https://api.kimi.com/coding"
    assert claude_model_for_account(coding) == "k3"
    assert codex_model_for_account(coding) == "k3"

    deepseek = ProviderAccount(
        id="ds-flash",
        base_url="https://api.deepseek.com/v1",
        api_key="x",
        note="flash lane",
    )
    assert detect_claude_template(deepseek) == "deepseek"
    assert claude_base_url_for_account(deepseek) == "https://api.deepseek.com/anthropic"
    assert claude_model_for_account(deepseek) == "deepseek-v4-flash"


def test_codex_import_from_agent(tmp_path: Path):
    store = CodexProfileStore(
        path=tmp_path / "codex.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    account = ProviderAccount(
        id="acct1",
        base_url="https://api.moonshot.cn/v1",
        api_key="sk-import-codex-key",
        api_key_env="KIMI_API_KEY",
        note="work kimi",
    )
    profile = store.import_from_account(account)
    assert profile.template == "kimi"
    assert profile.base_url.endswith("/v1")
    assert store.resolve_api_key(profile) == "sk-import-codex-key"


def test_codex_import_keeps_kimi_coding_url(tmp_path: Path):
    store = CodexProfileStore(
        path=tmp_path / "codex.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    account = ProviderAccount(
        id="coding1",
        base_url="https://api.kimi.com/coding/v1",
        api_key="sk-coding",
    )
    profile = store.import_from_account(account)
    assert profile.base_url == "https://api.kimi.com/coding/v1"
    assert profile.model == "k3"
    assert profile.wire_api == "responses"


def test_claude_import_from_agent_moonshot(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "claude.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    account = ProviderAccount(
        id="acct1",
        base_url="https://api.moonshot.cn/v1",
        api_key="sk-import-claude-key",
        api_key_env="KIMI_API_KEY",
    )
    profile = store.import_from_account(account)
    assert profile.template == "kimi-platform"
    assert profile.base_url == "https://api.moonshot.cn/anthropic"
    env = store.launch_env(profile)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.moonshot.cn/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-import-claude-key"
    assert "kimi" in CLAUDE_TEMPLATES
    assert "kimi-platform" in CLAUDE_TEMPLATES


def test_claude_import_kimi_coding_and_deepseek(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "claude.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    coding = ProviderAccount(
        id="kc",
        base_url="https://api.kimi.com/coding/v1",
        api_key="sk-coding",
    )
    profile = store.import_from_account(coding)
    assert profile.base_url.rstrip("/") == "https://api.kimi.com/coding"
    assert profile.model == "k3"
    env = store.launch_env(profile)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert env["ANTHROPIC_MODEL"] == "k3"
    assert env["ANTHROPIC_API_KEY"] == "sk-coding"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "k3"

    ds = ProviderAccount(
        id="ds",
        base_url="https://api.deepseek.com/v1",
        api_key="sk-ds",
    )
    ds_profile = store.import_from_account(ds)
    assert ds_profile.template == "deepseek"
    assert ds_profile.base_url == "https://api.deepseek.com/anthropic"
    assert "deepseek" in CLAUDE_TEMPLATES


def test_claude_resolve_falls_back_to_agent_account(tmp_path: Path):
    store = ClaudeProfileStore(
        path=tmp_path / "claude.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    profile = store.upsert(
        profile_id="kimi",
        name="Kimi",
        template="kimi",
        base_url="https://api.kimi.com/coding",
        model="k3",
    )
    accounts = [
        ProviderAccount(
            id="agent-kimi",
            base_url="https://api.kimi.com/coding/v1",
            api_key="sk-from-agent",
        )
    ]
    assert store.resolve_api_key(profile, agent_accounts=accounts) == "sk-from-agent"
