# Donate assets

Support / donate UI is **disabled** by default (`SUPPORT_UI_ENABLED = False`
in `aiharness/support.py`). Do not ship QR images in public builds.

When you re-enable support later, place payment QR images here **locally**
(they are gitignored and excluded from the PyInstaller bundle):

| File | Source |
|---|---|
| `alipay.png` | 支付宝 App → 收钱 → 保存收款码 |
| `wechat.png` | 微信 App → 我 → 服务 → 收付款 / 二维码收款 → 保存收款码 |
