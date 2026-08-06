"""First-run setup: no defaults, no guessing, only valid models selectable."""

from __future__ import annotations

import pytest

from aiharness.config.loader import EXAMPLE, load_config
from aiharness.config.schema import Config, ProviderAccount
from aiharness.setup import (
    AccountProbe,
    SetupError,
    assign_role,
    build_account,
    build_model,
    looks_like_reasoning_model,
    probe_and_list,
    readiness,
    role_table,
    suggest_alias,
    unassigned_roles,
)
from aiharness.ui.commands import dispatch

# -- the shipped config is empty -------------------------------------------


def test_the_example_config_configures_nothing(tmp_path):
    """No preset models, no preset accounts, no preset roles."""
    (tmp_path / ".aiharness.yaml").write_text(EXAMPLE, encoding="utf-8")
    config = load_config(tmp_path)
    assert config.accounts == []
    assert config.models == []
    assert config.roles == {}


def test_an_empty_config_is_reported_as_not_ready(tmp_path):
    ready, problems = readiness(Config())
    assert not ready
    assert any("no API accounts" in p for p in problems)
    assert any("main" in p for p in problems)


def test_no_vendor_names_are_preconfigured(tmp_path):
    """The example may mention vendors in comments, never in live keys."""
    (tmp_path / ".aiharness.yaml").write_text(EXAMPLE, encoding="utf-8")
    config = load_config(tmp_path)
    assert not config.accounts  # nothing live, whatever the comments say


# -- account construction --------------------------------------------------


def test_base_url_gains_a_version_suffix_when_missing():
    account = build_account("x", "https://api.example.com", "sk-1")
    assert account.base_url == "https://api.example.com/v1"


def test_an_explicit_version_path_is_left_alone():
    account = build_account("x", "https://api.example.com/v1", "sk-1")
    assert account.base_url == "https://api.example.com/v1"


@pytest.mark.parametrize("bad", ["api.example.com", "ftp://example.com", ""])
def test_a_url_without_http_is_refused(bad):
    with pytest.raises(SetupError):
        build_account("x", bad, "sk-1")


@pytest.mark.parametrize("bad", ["has space", "-leading", "", "with/slash"])
def test_a_bad_account_id_is_refused(bad):
    with pytest.raises(SetupError):
        build_account(bad, "https://api.example.com/v1", "sk-1")


# -- probing ---------------------------------------------------------------


async def test_probe_lists_the_models_an_account_serves(fake):
    account = ProviderAccount(id="p", base_url=fake.base_url, api_key="k")
    probe = await probe_and_list(account)
    assert probe.ok
    assert "fake-model" in probe.models


async def test_probe_reports_an_unreachable_endpoint():
    account = ProviderAccount(
        id="p", base_url="http://127.0.0.1:1/v1", api_key="k", timeout=1.0
    )
    probe = await probe_and_list(account)
    assert not probe.ok
    assert probe.detail


@pytest.mark.parametrize(
    ("setting", "expected"),
    [("", "直连"), ("http://127.0.0.1:7897", "7897"), ("direct", "不受代理影响")],
)
async def test_a_failed_probe_says_what_the_proxy_setting_was(setting, expected):
    """A wrong proxy and a dead endpoint raise nearly the same error.

    The difference decides whether the fix is in this dialog or somewhere
    else entirely, so the account's route is named in the failure text.
    """
    account = ProviderAccount(
        id="p", base_url="http://127.0.0.1:1/v1", api_key="k", timeout=1.0, proxy=setting
    )
    probe = await probe_and_list(account)
    assert not probe.ok
    assert expected in probe.detail


def test_non_chat_models_are_filtered_from_the_offer():
    probe = AccountProbe(
        ok=True,
        detail="",
        models=[
            "gpt-4o", "text-embedding-3-small", "whisper-1",
            "dall-e-3", "deepseek-chat", "bge-reranker",
        ],
    )
    assert probe.chat_models == ["gpt-4o", "deepseek-chat"]


# -- model construction ----------------------------------------------------


def test_alias_is_derived_from_the_tail_of_a_namespaced_id():
    assert suggest_alias("deepseek-ai/DeepSeek-V3", set()) == "deepseek-v3"


def test_alias_collisions_are_resolved():
    assert suggest_alias("vendor/chat", {"chat"}) == "chat-2"
    assert suggest_alias("vendor/chat", {"chat", "chat-2"}) == "chat-3"


@pytest.mark.parametrize(
    "model_id, reasoning",
    [
        ("deepseek-reasoner", True),
        ("deepseek-r1", True),
        ("o3-mini", True),
        ("qwq-32b", True),
        ("deepseek-chat", False),
        ("gpt-4o-mini", False),
    ],
)
def test_reasoning_models_are_recognised(model_id, reasoning):
    assert looks_like_reasoning_model(model_id) is reasoning


def test_a_reasoning_model_gets_effort_levels():
    model = build_model("r", "deepseek-reasoner", ["acc"])
    assert model.effort.mode == "reasoning_effort"
    assert model.effort_levels() == ["low", "medium", "high"]


def test_a_plain_model_gets_no_effort_parameter():
    model = build_model("c", "deepseek-chat", ["acc"])
    assert model.effort.mode == "none"
    assert model.effort_levels() == []


def test_pricing_is_left_at_zero_rather_than_guessed():
    """A guessed price produces a confidently wrong cost readout."""
    model = build_model("c", "some-new-model", ["acc"])
    assert model.pricing.input == 0.0
    assert model.pricing.output == 0.0


def test_a_model_needs_an_account():
    with pytest.raises(SetupError):
        build_model("c", "some-model", [])


# -- role assignment: only valid models ------------------------------------


def make_config() -> Config:
    config = Config()
    config.accounts.append(build_account("a1", "https://x.example.com/v1", "k"))
    config.accounts.append(build_account("a2", "https://y.example.com/v1", "k"))
    config.models.append(build_model("chat", "vendor-chat", ["a1"]))
    return config


def test_a_role_can_be_bound_to_a_configured_model():
    config = make_config()
    binding = assign_role(config, "main", "chat")
    assert binding.model == "chat"
    assert config.roles["main"].model == "chat"


def test_a_role_pointing_at_an_unconfigured_model_is_refused():
    """This is the 'only valid models are selectable' guarantee."""
    config = make_config()
    with pytest.raises(SetupError) as error:
        assign_role(config, "main", "not-configured")
    assert "Configured models: chat" in str(error.value)
    assert "main" not in config.roles  # nothing was written


def test_a_role_pointing_at_an_unknown_account_is_refused():
    config = make_config()
    with pytest.raises(SetupError):
        assign_role(config, "main", "chat@ghost")


def test_a_role_pinning_an_account_that_does_not_serve_the_model_is_refused():
    config = make_config()
    with pytest.raises(SetupError) as error:
        assign_role(config, "main", "chat@a2")
    assert "does not serve" in str(error.value)


def test_unassigned_roles_are_listed():
    config = make_config()
    assign_role(config, "main", "chat")
    remaining = unassigned_roles(config)
    assert "main" not in remaining
    assert "cheap" in remaining


def test_role_table_shows_inheritance_from_main():
    config = make_config()
    assign_role(config, "main", "chat")
    rows = {role: (binding, explicit) for role, binding, explicit in role_table(config)}
    assert rows["main"][1] is True
    assert rows["cheap"][1] is False
    assert "默认对话模型" in rows["cheap"][0]


def test_readiness_needs_only_main():
    config = make_config()
    assert not readiness(config)[0]
    assign_role(config, "main", "chat")
    assert readiness(config)[0]


# -- through the UI --------------------------------------------------------


@pytest.fixture
def blank_app(workspace, sessions):
    """An app with nothing configured, as a first run really is."""
    from aiharness.ui.app import HarnessApp

    return HarnessApp(Config(), workspace)


async def test_the_ui_starts_with_nothing_configured(blank_app):
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        assert blank_app.config.models == []
        output = await dispatch(blank_app, "/setup")
        assert "/accounts add" in output


async def test_adding_an_account_checks_it_before_saving(blank_app, fake, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-live-value")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(
            blank_app, f"/accounts add acc {fake.base_url} TEST_KEY"
        )
        assert "Added **acc**" in output
        assert blank_app.config.account("acc") is not None


async def test_an_unreachable_account_is_not_saved(blank_app, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-value")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(
            blank_app, "/accounts add dead http://127.0.0.1:1/v1 TEST_KEY"
        )
        assert "Not saved" in output
        assert blank_app.config.account("dead") is None


async def test_a_missing_env_var_is_reported(blank_app):
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(
            blank_app, "/accounts add acc https://example.com/v1 NOT_SET_ANYWHERE"
        )
        assert "not set in this environment" in output
        assert blank_app.config.accounts == []


async def test_the_key_never_reaches_the_saved_file(blank_app, fake, monkeypatch, tmp_path):
    """The secret must not be written to disk — only its env reference."""
    from aiharness.config.loader import save_config

    monkeypatch.setenv("TEST_KEY", "sk-super-secret")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        await dispatch(blank_app, f"/accounts add acc {fake.base_url} TEST_KEY")

        # The live object holds the real key, because requests need it.
        assert blank_app.config.account("acc").api_key == "sk-super-secret"

        path = save_config(blank_app.config, tmp_path / "config.yaml")
        written = path.read_text(encoding="utf-8")
        assert "sk-super-secret" not in written
        assert "${TEST_KEY}" in written


async def test_saving_does_not_damage_the_live_config(blank_app, fake, monkeypatch, tmp_path):
    """Writing the file must not leave the running session keyless."""
    from aiharness.config.loader import save_config

    monkeypatch.setenv("TEST_KEY", "sk-super-secret")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        await dispatch(blank_app, f"/accounts add acc {fake.base_url} TEST_KEY")
        save_config(blank_app.config, tmp_path / "config.yaml")
        assert blank_app.config.account("acc").api_key == "sk-super-secret"


def test_a_saved_config_round_trips_through_the_environment(tmp_path, monkeypatch):
    """The reference in the file resolves back to the key on load."""
    from aiharness.config.loader import load_config, save_config

    monkeypatch.setenv("ROUND_TRIP_KEY", "sk-round-trip")
    config = Config()
    config.accounts.append(
        build_account("a", "https://x.example.com/v1", "sk-round-trip",
                      api_key_env="ROUND_TRIP_KEY")
    )
    save_config(config, tmp_path / ".aiharness.yaml")
    reloaded = load_config(tmp_path)
    assert reloaded.account("a").api_key == "sk-round-trip"


async def test_models_add_lists_what_the_account_serves(blank_app, fake, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        await dispatch(blank_app, f"/accounts add acc {fake.base_url} TEST_KEY")
        output = await dispatch(blank_app, "/models add acc")
        assert "fake-model" in output


async def test_a_model_can_be_added_and_bound(blank_app, fake, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        await dispatch(blank_app, f"/accounts add acc {fake.base_url} TEST_KEY")
        await dispatch(blank_app, "/models add acc fake-model mymodel")
        assert blank_app.config.model("mymodel") is not None

        output = await dispatch(blank_app, "/role main mymodel")
        assert "main" in output
        assert blank_app.config.roles["main"].model == "mymodel"


async def test_the_ui_refuses_a_role_for_an_unconfigured_model(blank_app):
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        output = await dispatch(blank_app, "/role main something-invented")
        assert "no model" in output.lower()
        assert "main" not in blank_app.config.roles


async def test_a_model_still_bound_to_a_role_cannot_be_removed(blank_app, fake, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        await dispatch(blank_app, f"/accounts add acc {fake.base_url} TEST_KEY")
        await dispatch(blank_app, "/models add acc fake-model m")
        await dispatch(blank_app, "/role main m")
        output = await dispatch(blank_app, "/models rm m")
        assert "still bound" in output
        assert blank_app.config.model("m") is not None


async def test_an_account_still_serving_models_cannot_be_removed(blank_app, fake, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        await dispatch(blank_app, f"/accounts add acc {fake.base_url} TEST_KEY")
        await dispatch(blank_app, "/models add acc fake-model m")
        output = await dispatch(blank_app, "/accounts rm acc")
        assert "still serves" in output


async def test_config_save_refuses_a_broken_configuration(blank_app):
    async with blank_app.run_test() as pilot:
        await pilot.pause()
        blank_app.config.roles["main"] = type(
            "B", (), {"model": "ghost", "account": None, "effort": None,
                      "context": None, "temperature": None, "describe": lambda s: "ghost"}
        )()
        output = await dispatch(blank_app, "/config save")
        assert "Not saving" in output
