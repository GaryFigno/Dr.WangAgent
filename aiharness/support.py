"""Public support / sponsorship links shown in README and the settings UI.

Edit these before publishing. Keep real API keys out of this module — only
URLs and relative asset paths belong here.
"""

from __future__ import annotations

#: GitHub Sponsors page. Empty string hides the button until you set it.
#: TODO: replace YOUR_GITHUB_USERNAME with your real username before the
#: first public build / push (README badge and .github/FUNDING.yml too).
#: Example: ``https://github.com/sponsors/your-username``
GITHUB_SPONSORS_URL = "https://github.com/sponsors/YOUR_GITHUB_USERNAME"

#: Optional Chinese platforms (爱发电 etc.). Empty entries are hidden.
CUSTOM_DONATE_URLS: list[tuple[str, str]] = [
    # ("爱发电", "https://afdian.com/a/your-id"),
]

#: Served from the GUI static root when the file exists.
ALIPAY_QR_STATIC = "/static/donate/alipay.png"
WECHAT_QR_STATIC = "/static/donate/wechat.png"


def public_support() -> dict:
    """Payload the GUI can render without reading the filesystem for URLs."""
    from . import __version__

    links = []
    if GITHUB_SPONSORS_URL.strip():
        links.append({"id": "github_sponsors", "label": "GitHub Sponsors", "url": GITHUB_SPONSORS_URL.strip()})
    for label, url in CUSTOM_DONATE_URLS:
        if label and url:
            links.append({"id": "custom", "label": label, "url": url})
    return {
        "version": __version__,
        "links": links,
        "alipay_qr": ALIPAY_QR_STATIC,
        "wechat_qr": WECHAT_QR_STATIC,
    }
