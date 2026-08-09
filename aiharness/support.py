"""Public support / sponsorship links for the settings UI.

Disabled for now — do not expose Sponsors or payment QR codes until ready.
Set ``SUPPORT_UI_ENABLED = True`` and fill the URLs/assets when you want them back.
"""

from __future__ import annotations

#: Master switch for the in-app "支持作者" entry and donate payload.
SUPPORT_UI_ENABLED = False

#: GitHub Sponsors page. Empty string hides the button.
GITHUB_SPONSORS_URL = ""

#: Optional Chinese platforms (爱发电 etc.). Empty entries are hidden.
CUSTOM_DONATE_URLS: list[tuple[str, str]] = [
    # ("爱发电", "https://afdian.com/a/your-id"),
]

#: Served from the GUI static root when the file exists AND support UI is enabled.
ALIPAY_QR_STATIC = "/static/donate/alipay.png"
WECHAT_QR_STATIC = "/static/donate/wechat.png"


def public_support() -> dict:
    """Payload the GUI can render without reading the filesystem for URLs."""
    from . import __version__

    if not SUPPORT_UI_ENABLED:
        return {
            "version": __version__,
            "enabled": False,
            "links": [],
            "alipay_qr": "",
            "wechat_qr": "",
        }

    links = []
    if GITHUB_SPONSORS_URL.strip():
        links.append(
            {
                "id": "github_sponsors",
                "label": "GitHub Sponsors",
                "url": GITHUB_SPONSORS_URL.strip(),
            }
        )
    for label, url in CUSTOM_DONATE_URLS:
        if label and url:
            links.append({"id": "custom", "label": label, "url": url})
    return {
        "version": __version__,
        "enabled": True,
        "links": links,
        "alipay_qr": ALIPAY_QR_STATIC,
        "wechat_qr": WECHAT_QR_STATIC,
    }
