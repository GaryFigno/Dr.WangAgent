"""Per-account proxy selection.

One machine often needs two answers at once. A proxy like Clash Verge is what
makes a foreign endpoint reachable, and the same proxy is what makes a
domestic one slow, or fails it outright by routing Beijing traffic through
Los Angeles. Because ``HTTP_PROXY`` is a *process*-wide setting, httpx's
default of trusting it applies that one answer to every account.

So the choice is moved onto the account:

* :data:`INHERIT` (the default) keeps today's behaviour — whatever the
  environment says. Nothing changes for anyone who has not touched this.
* :data:`DIRECT` ignores the environment entirely, for endpoints that are
  reachable without help and are slower through a tunnel.
* anything else is a proxy URL used for that account alone.

The environment is only ever *ignored*, never edited: mutating ``os.environ``
to steer one request would race with every other request in flight.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..config.schema import ProviderAccount

#: Follow ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY``, as httpx does.
INHERIT = ""
#: Bypass every proxy, including the ones in the environment.
DIRECT = "direct"

#: Schemes httpx can proxy through. socks5 additionally needs ``httpx[socks]``.
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
#: Ports the common Windows clients listen on, offered in the UI as hints.
COMMON_PROXY_URLS = (
    "http://127.0.0.1:7897",  # Clash Verge, mixed port
    "http://127.0.0.1:7890",  # Clash for Windows, mixed port
    "http://127.0.0.1:10809",  # v2rayN, http port
    "http://127.0.0.1:1080",  # generic socks/http
)


class ProxyError(ValueError):
    """Raised when a proxy setting cannot be used as written."""


def normalise(raw: str) -> str:
    """Validate a proxy setting and return its canonical form.

    Args:
      raw: What the user typed: blank, ``direct``, or a URL. A bare
        ``127.0.0.1:7897`` is accepted and given an ``http://`` scheme,
        because that is how proxy clients display their own address.

    Returns:
      :data:`INHERIT`, :data:`DIRECT`, or a full proxy URL.

    Raises:
      ProxyError: If the value is neither of the keywords nor a usable URL.
    """
    text = raw.strip()
    if not text:
        return INHERIT
    if text.lower() in (DIRECT, "none", "off", "直连"):
        return DIRECT

    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in PROXY_SCHEMES:
        raise ProxyError(
            f"不认识的代理协议 '{parsed.scheme}'，支持 {'、'.join(PROXY_SCHEMES)}"
        )
    if not parsed.hostname:
        raise ProxyError("代理地址缺少主机名")
    if parsed.port is None:
        raise ProxyError("代理地址要带端口，例如 http://127.0.0.1:7897")
    return text


def client_kwargs(account: ProviderAccount) -> dict[str, Any]:
    """The httpx arguments implementing this account's proxy choice.

    Every client the app builds for an account goes through here, so a
    request cannot accidentally keep the process-wide default just because it
    was constructed somewhere new.
    """
    setting = (account.proxy or INHERIT).strip()
    if not setting:
        return {"trust_env": True}
    if setting.lower() == DIRECT:
        # trust_env=False is the part that actually matters: without it httpx
        # would still pick the proxy up from the environment.
        return {"trust_env": False}
    return {"trust_env": False, "proxy": setting}


def describe(account: ProviderAccount, chinese: bool = True) -> str:
    """A short human label for the account's routing, for the settings list."""
    setting = (account.proxy or INHERIT).strip()
    if not setting:
        return "跟随系统" if chinese else "system"
    if setting.lower() == DIRECT:
        return "直连" if chinese else "direct"
    return setting


def explain_failure(account: ProviderAccount, error: Exception) -> str:
    """Turn a connection failure into advice about the proxy, when relevant.

    A wrong proxy setting and an unreachable endpoint produce almost the same
    exception, and the difference matters: one is fixed in this dialog, the
    other is not.
    """
    setting = (account.proxy or INHERIT).strip()
    base = str(error)
    if setting and setting.lower() != DIRECT:
        return f"{base}（该账号走代理 {setting}，先确认代理开着且端口对）"
    if not setting:
        return f"{base}（该账号跟随系统代理，可试试把它改成「直连」）"
    return f"{base}（该账号已设为直连，不受代理影响）"
