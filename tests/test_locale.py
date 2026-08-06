"""GUI language codes and settings persistence."""

from aiharness.gui.locale import (
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    language_choices,
    normalize_language,
)


def test_supported_languages_include_common_ones():
    for code in ("auto", "zh", "en", "ja", "ko", "es", "de", "fr"):
        assert code in SUPPORTED_LANGUAGES
        assert code in LANGUAGE_LABELS


def test_normalize_language_aliases():
    assert normalize_language("zh-CN") == "zh"
    assert normalize_language("zh_TW") == "zh-TW"
    assert normalize_language("jp") == "ja"
    assert normalize_language("unknown") == "auto"
    assert normalize_language("en-US") == "en"


def test_language_choices_are_wire_safe():
    choices = language_choices()
    assert choices[0]["code"] == "auto"
    assert all("label" in item for item in choices)
