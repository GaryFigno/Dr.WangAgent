# Donate assets

Place payment QR images here **locally** for packaging:

| File | Source |
|---|---|
| `alipay.png` | 支付宝 App → 收钱 → 保存收款码 |
| `wechat.png` | 微信 App → 我 → 服务 → 收付款 / 二维码收款 → 保存收款码 |

Tips:

1. Crop extra chrome so the QR is clear and roughly square.
2. Keep filenames exactly as above (lowercase).
3. Do **not** put API keys or other secrets in this folder.

> **These two files are git-ignored and will never be published.**
> They exist only on your machine so the packaged app can show the payment QR
> codes in Settings → About & support → "Support the author". If you want the
> codes public, that is your call — but remember GitHub history is forever:
> once pushed, a fork keeps them even after deletion.
