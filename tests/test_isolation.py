"""The test suite must never touch the developer's real profile.

Both of the bugs this guards against were invisible: the suite passed while
overwriting ``config.yaml``, and passed again while writing stale pytest
directories into the recent-projects list that then appeared in the real UI.
A green run is not evidence of isolation, so it is asserted directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: Every function in the package that resolves a user-level path.
LOCATORS = [
    ("aiharness.config.loader", "default_config_path"),
    ("aiharness.credentials", "credentials_path"),
    ("aiharness.gui.drafts", "drafts_path"),
    ("aiharness.gui.workspace", "recents_path"),
    ("aiharness.rules", "global_rules_dir"),
    ("aiharness.scheduler.jobs", "jobs_path"),
    ("aiharness.session.store", "sessions_root"),
    ("aiharness.ui.prefs", "prefs_path"),
    ("aiharness.skills", "user_skill_root"),
]


def real_home() -> list[Path]:
    """The directories the app uses outside a test."""
    from platformdirs import user_config_dir, user_data_dir

    return [
        Path(user_config_dir("aiharness", appauthor=False)),
        Path(user_data_dir("aiharness", appauthor=False)),
    ]


@pytest.mark.parametrize(("module_name", "function"), LOCATORS)
def test_no_user_path_resolves_into_the_real_profile(module_name, function, tmp_path):
    """Every locator must point somewhere disposable while tests run."""
    import importlib

    module = importlib.import_module(module_name)
    resolved = Path(getattr(module, function)()).resolve()

    for home in real_home():
        assert home not in resolved.parents and resolved != home, (
            f"{module_name}.{function}() resolves to {resolved}, inside the "
            f"real profile at {home}"
        )


def test_every_platformdirs_user_is_covered_by_a_locator():
    """A new module calling platformdirs must be added to LOCATORS.

    This is the test that would have caught ``workspaces.json``: the file was
    written by a module nobody had listed, so nothing checked it.
    """
    import aiharness  # noqa: F401  (ensures the package is imported)

    users = {
        name
        for name, module in sys.modules.items()
        if name.startswith("aiharness")
        and (hasattr(module, "user_config_dir") or hasattr(module, "user_data_dir"))
    }
    listed = {module_name for module_name, _ in LOCATORS}
    assert users <= listed, (
        f"these modules resolve user paths but are not covered: {sorted(users - listed)}"
    )
