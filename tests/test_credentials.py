"""Keys must never reach ``config.yaml``, whichever way they arrived.

This file exists because the invariant has been broken twice by ordinary
refactors, both times silently: the config still worked, the app still ran,
and the only symptom was a secret sitting in a file people share.
"""

from __future__ import annotations

import pytest

from aiharness.config.loader import load_config, save_config
from aiharness.config.schema import Config, ProviderAccount
from aiharness.credentials import CredentialStore, classify

SECRET = "sk-31bc7c239a2645e296b0a0949e531418"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the config and credential files somewhere disposable."""
    monkeypatch.setenv("AIH_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("AIH_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))
    return tmp_path


# -- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SECRET, "secret"),
        ("sk-proj-AAAAAAAAAAAAAAAAAAAAAA", "secret"),
        ("gsk_0123456789abcdefghij", "secret"),
        ("DEEPSEEK_API_KEY", "env"),
        ("OPENAI_API_KEY", "env"),
        ("MY_KEY", "env"),
        ("", "unknown"),
        ("my key", "unknown"),
        ("abc", "unknown"),
    ],
)
def test_the_two_shapes_are_told_apart(text, expected):
    assert classify(text) == expected


def test_an_uppercase_key_is_still_read_as_a_key():
    """Prefix beats shape: ``SK-...`` is not an environment variable name."""
    assert classify("sk-AAAAAAAAAAAAAAAAAAAA") == "secret"


# -- the file invariant ----------------------------------------------------


def test_a_pasted_key_never_reaches_the_config_file(isolated):
    cfg = Config()
    cfg.accounts.append(
        ProviderAccount(id="paste", base_url="https://api.example.com/v1", api_key=SECRET)
    )
    CredentialStore().put("paste", SECRET)

    written = save_config(cfg)
    assert SECRET not in written.read_text(encoding="utf-8")

    # …and it still comes back on the way in.
    assert load_config().account("paste").api_key == SECRET


def test_an_env_backed_key_is_stored_as_a_reference(isolated, monkeypatch):
    monkeypatch.setenv("AIH_TEST_KEY", SECRET)
    cfg = Config()
    cfg.accounts.append(
        ProviderAccount(
            id="env",
            base_url="https://api.example.com/v1",
            api_key=SECRET,
            api_key_env="AIH_TEST_KEY",
        )
    )

    text = save_config(cfg).read_text(encoding="utf-8")
    assert SECRET not in text
    assert "${AIH_TEST_KEY}" in text
    assert load_config().account("env").api_key == SECRET


def test_the_live_object_is_not_damaged_by_saving(isolated):
    """Saving must not blank the key on the config the app is still using."""
    cfg = Config()
    cfg.accounts.append(
        ProviderAccount(id="live", base_url="https://api.example.com/v1", api_key=SECRET)
    )
    save_config(cfg)
    assert cfg.account("live").api_key == SECRET


def test_no_account_shape_can_smuggle_a_key_onto_disk(isolated):
    """The guard is unconditional, so a new way to supply a key stays safe."""
    variants = [
        ProviderAccount(id="a", base_url="u", api_key=SECRET),
        ProviderAccount(id="b", base_url="u", api_key=SECRET, api_key_env="X"),
        ProviderAccount(id="c", base_url="u", api_key=SECRET, enabled=False),
        ProviderAccount(id="d", base_url="u", api_key=SECRET, note="prod"),
    ]
    for account in variants:
        assert SECRET not in account.for_storage().api_key


# -- the store itself ------------------------------------------------------


def test_keys_round_trip_and_can_be_removed(isolated):
    store = CredentialStore()
    store.put("one", SECRET)
    store.put("two", "sk-second-key-value-here")

    assert store.get("one") == SECRET
    assert store.ids() == ["one", "two"]
    assert store.remove("one") is True
    assert store.get("one") == ""
    assert store.remove("one") is False


def test_a_corrupt_store_reads_as_empty_rather_than_crashing(isolated):
    """A truncated sync or a hand-edit must not stop the app from starting."""
    store = CredentialStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.get("anything") == ""
    assert store.ids() == []
