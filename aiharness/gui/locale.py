"""GUI / UI language codes and labels (shared by backend and settings)."""

from __future__ import annotations

#: Codes offered in Settings. ``auto`` follows the browser / OS when possible.
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "auto",
    "zh",
    "zh-TW",
    "en",
    "ja",
    "ko",
    "es",
    "fr",
    "de",
    "pt",
    "ru",
    "vi",
    "id",
)

#: Native labels for the language picker (always shown in that language).
LANGUAGE_LABELS: dict[str, str] = {
    "auto": "Auto / 自动",
    "zh": "简体中文",
    "zh-TW": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português",
    "ru": "Русский",
    "vi": "Tiếng Việt",
    "id": "Bahasa Indonesia",
}


def normalize_language(code: str | None) -> str:
    """Return a supported language code, defaulting to ``auto``."""
    raw = (code or "auto").strip()
    if raw in SUPPORTED_LANGUAGES:
        return raw
    lowered = raw.lower().replace("_", "-")
    aliases = {
        "zh-cn": "zh",
        "zh-hans": "zh",
        "zh-hant": "zh-TW",
        "zh-hk": "zh-TW",
        "zh-tw": "zh-TW",
        "jp": "ja",
        "eng": "en",
        "korean": "ko",
    }
    if lowered in aliases:
        return aliases[lowered]
    base = lowered.split("-", 1)[0]
    if base in SUPPORTED_LANGUAGES:
        return base
    return "auto"


def language_choices() -> list[dict[str, str]]:
    """Wire-safe list for the settings dropdown."""
    return [
        {"code": code, "label": LANGUAGE_LABELS.get(code, code)}
        for code in SUPPORTED_LANGUAGES
    ]
