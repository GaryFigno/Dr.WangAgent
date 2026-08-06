"""Storage for API keys that were pasted rather than exported.

The config file must never contain a secret: people put configs in git, paste
them into issues, and sync them between machines. But demanding that every
key arrive through an environment variable is its own kind of hostile — the
key is in the clipboard, not in the environment, and telling somebody to set
a variable and restart before they can try the app is a bad first minute.

So both are supported, and the file layout keeps them apart:

* an **environment reference** is written into the config as ``${NAME}`` and
  resolved at load time;
* a **pasted key** is written here instead, in a separate file with the
  narrowest permissions the platform allows, and the config records only that
  the key lives in the store.

Either way ``config.yaml`` stays safe to share.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from platformdirs import user_config_dir

CREDENTIALS_FILE = "credentials.json"
#: Owner read/write only. Meaningful on POSIX; a no-op on Windows, where the
#: user profile directory is already restricted.
OWNER_ONLY = stat.S_IRUSR | stat.S_IWUSR

#: An environment variable name: capitals, digits and underscores. Real keys
#: never look like this, which is what makes the two distinguishable.
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
#: Prefixes used by the vendors people actually paste.
KEY_PREFIXES = ("sk-", "sk_", "gsk_", "xai-", "pplx-", "hf_", "ak-", "Bearer ")
#: Below this length a string is more likely a typo than a key.
MIN_KEY_LENGTH = 16


def credentials_path() -> Path:
    override = os.environ.get("AIH_CREDENTIALS_FILE")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("aiharness", appauthor=False)) / CREDENTIALS_FILE


def looks_like_env_name(text: str) -> bool:
    """Whether a string is plausibly the *name* of an environment variable."""
    return bool(ENV_NAME_RE.match(text.strip()))


def looks_like_secret(text: str) -> bool:
    """Whether a string is plausibly an API key itself."""
    cleaned = text.strip()
    if not cleaned or " " in cleaned:
        return False
    if cleaned.startswith(KEY_PREFIXES):
        return True
    # Long, mixed-case or digit-bearing, and not shaped like a variable name.
    return len(cleaned) >= MIN_KEY_LENGTH and not looks_like_env_name(cleaned)


def classify(text: str) -> str:
    """Decide what the user typed.

    Returns:
      ``"env"`` when it names an environment variable, ``"secret"`` when it
      is the key itself, or ``"unknown"`` when it is neither.
    """
    cleaned = text.strip()
    if not cleaned:
        return "unknown"
    if looks_like_env_name(cleaned) and not cleaned.startswith(KEY_PREFIXES):
        return "env"
    if looks_like_secret(cleaned):
        return "secret"
    return "unknown"


class CredentialStore:
    """Pasted keys, kept out of the config file."""

    def __init__(self, path: Path | None = None):
        self.path = path or credentials_path()

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        keys = payload.get("keys")
        return {str(k): str(v) for k, v in keys.items()} if isinstance(keys, dict) else {}

    def _write(self, keys: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"keys": keys}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            self.path.chmod(OWNER_ONLY)
        except OSError:  # pragma: no cover - platform dependent
            pass

    def get(self, account_id: str) -> str:
        return self._read().get(account_id, "")

    def put(self, account_id: str, key: str) -> None:
        keys = self._read()
        keys[account_id] = key
        self._write(keys)

    def remove(self, account_id: str) -> bool:
        keys = self._read()
        if account_id not in keys:
            return False
        del keys[account_id]
        self._write(keys)
        return True

    def ids(self) -> list[str]:
        return sorted(self._read())
