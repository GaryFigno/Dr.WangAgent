"""Per-account proxy routing.

The point of this feature is that one machine needs two answers at once: a
proxy makes a foreign endpoint reachable and makes a domestic one slow. So
the tests are mostly about *isolation* — one account's route must not leak
into another's, and the process-wide environment must not override either.
"""

from __future__ import annotations

import pytest

from aiharness.config.schema import ProviderAccount
from aiharness.providers import proxy

CLASH = "http://127.0.0.1:7897"


def account(setting: str = "") -> ProviderAccount:
    return ProviderAccount(id="a", base_url="https://api.example.com/v1", proxy=setting)


# -- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", proxy.INHERIT),
        ("   ", proxy.INHERIT),
        ("direct", proxy.DIRECT),
        ("DIRECT", proxy.DIRECT),
        ("直连", proxy.DIRECT),
        ("none", proxy.DIRECT),
        (CLASH, CLASH),
        ("  http://127.0.0.1:7890  ", "http://127.0.0.1:7890"),
        ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
    ],
)
def test_settings_normalise(raw, expected):
    assert proxy.normalise(raw) == expected


def test_a_bare_host_and_port_gains_a_scheme():
    """Proxy clients display ``127.0.0.1:7897``; typing that must work."""
    assert proxy.normalise("127.0.0.1:7897") == CLASH


@pytest.mark.parametrize(
    "bad", ["ftp://127.0.0.1:21", "http://127.0.0.1", "http://:7897", "gopher://x:70"]
)
def test_unusable_settings_are_refused_rather_than_guessed(bad):
    with pytest.raises(proxy.ProxyError):
        proxy.normalise(bad)


# -- the httpx arguments ---------------------------------------------------


def test_blank_follows_the_environment():
    """The default must not change behaviour for anyone who never touches it."""
    assert proxy.client_kwargs(account()) == {"trust_env": True}


def test_direct_ignores_the_environment():
    """``trust_env=False`` is the part that matters.

    Without it httpx reads ``HTTP_PROXY`` itself, so an account marked direct
    would still be tunnelled — the exact bug this feature exists to fix.
    """
    kwargs = proxy.client_kwargs(account("direct"))
    assert kwargs == {"trust_env": False}
    assert "proxy" not in kwargs


def test_an_explicit_proxy_also_ignores_the_environment():
    kwargs = proxy.client_kwargs(account(CLASH))
    assert kwargs == {"trust_env": False, "proxy": CLASH}


def test_two_accounts_keep_separate_routes(monkeypatch):
    """The environment is read, never written; one account cannot move another."""
    monkeypatch.setenv("HTTPS_PROXY", CLASH)
    tunnelled = proxy.client_kwargs(account(CLASH))
    straight = proxy.client_kwargs(account("direct"))
    inherited = proxy.client_kwargs(account())

    assert tunnelled["proxy"] == CLASH
    assert straight["trust_env"] is False and "proxy" not in straight
    assert inherited["trust_env"] is True
    # Nothing edited the process environment to achieve that.
    import os

    assert os.environ["HTTPS_PROXY"] == CLASH


# -- what the user is told -------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "shown"), [("", "跟随系统"), ("direct", "直连"), (CLASH, CLASH)]
)
def test_the_route_is_described_in_the_settings_list(setting, shown):
    assert proxy.describe(account(setting)) == shown


def test_a_failure_names_the_route_that_produced_it():
    error = OSError("All connection attempts failed")
    assert "7897" in proxy.explain_failure(account(CLASH), error)
    assert "直连" in proxy.explain_failure(account(), error)
    assert "不受代理影响" in proxy.explain_failure(account("direct"), error)


# -- the router's client pool ---------------------------------------------


async def test_changing_a_proxy_discards_the_pooled_client(config):
    """A pooled client has its route baked in at construction.

    Without dropping it, a proxy change appears to do nothing until restart.
    """
    from aiharness.providers.router import Router

    router = Router(config)
    first = router._client_for(config.accounts[0])
    assert router._client_for(config.accounts[0]) is first, "clients are pooled"

    await router.reset_client(config.accounts[0].id)
    assert router._client_for(config.accounts[0]) is not first
    assert first.is_closed
