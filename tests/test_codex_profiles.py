"""Tests for Codex multi-provider profiles."""

from __future__ import annotations

from pathlib import Path

from aiharness.credentials import CredentialStore
from aiharness.gui.codex_profiles import CodexProfileStore, TEMPLATES


def test_seed_defaults(tmp_path: Path):
    store = CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    ids = {p.id for p in store.profiles}
    assert {"kimi", "glm", "gemini", "grok"} <= ids
    assert store.active_id == "kimi"


def test_upsert_secret_and_materialize(tmp_path: Path):
    store = CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    profile = store.upsert(
        profile_id="kimi-work",
        name="Kimi Work",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.5",
        template="kimi",
        api_key="sk-test-key-1234567890abcd",
        make_active=True,
    )
    assert store.active_id == "kimi-work"
    assert store.resolve_api_key(profile).startswith("sk-test")
    home = store.materialize(profile)
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "model_provider" in text
    assert 'wire_api = "responses"' in text
    assert 'wire_api = "chat"' not in text
    assert "sk-test" not in text
    env = store.launch_env(profile)
    assert env["CODEX_HOME"] == str(home)
    assert env.get("KIMI_API_KEY", "").startswith("sk-test")


def test_repair_homes_rewrites_chat_wire_api(tmp_path: Path, monkeypatch):
    store = CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    home = tmp_path / ".codex-aih" / "kimi"
    home.mkdir(parents=True)
    config = home / "config.toml"
    config.write_text('wire_api = "chat"\n', encoding="utf-8")
    import aiharness.gui.codex_profiles as mod

    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    fixed = store.repair_homes()
    assert fixed >= 1
    assert 'wire_api = "responses"' in config.read_text(encoding="utf-8")


def test_templates_cover_vendors():
    for key in ("kimi", "glm", "gemini", "grok", "custom"):
        assert key in TEMPLATES
        assert TEMPLATES[key]["base_url"]
        assert TEMPLATES[key]["model"]


def test_materialize_base_url_override(tmp_path: Path):
    store = CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    profile = store.upsert(
        profile_id="coding",
        name="Coding",
        base_url="https://api.kimi.com/coding/v1",
        model="k3",
        template="kimi",
        api_key="sk-test-key-1234567890abcd",
    )
    home = store.materialize(profile, base_url_override="http://127.0.0.1:9/v1")
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:9/v1" in text
    assert "api.kimi.com" not in text
    assert profile.base_url == "https://api.kimi.com/coding/v1"


def test_profile_proxy_direct_and_clash(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://system-proxy:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://system-proxy:1")
    store = CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    direct = store.upsert(
        profile_id="kimi-direct",
        name="Kimi Direct",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.5",
        template="kimi",
        proxy="direct",
    )
    via = store.upsert(
        profile_id="kimi-clash",
        name="Kimi Clash",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.5",
        template="kimi",
        proxy="http://127.0.0.1:7897",
    )
    env_direct = store.launch_env(direct)
    assert "HTTP_PROXY" not in env_direct
    assert "HTTPS_PROXY" not in env_direct
    env_via = store.launch_env(via)
    assert env_via["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert env_via["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert direct.public()["proxy_label"] == "直连"


def test_delete_profile(tmp_path: Path):
    store = CodexProfileStore(
        path=tmp_path / "profiles.json",
        credentials=CredentialStore(path=tmp_path / "creds.json"),
    )
    store.upsert(
        profile_id="extra",
        name="Extra",
        base_url="https://example.com/v1",
        model="m",
        template="custom",
        make_active=True,
    )
    assert store.delete("extra")
    assert store.get("extra") is None
    assert store.active_id
