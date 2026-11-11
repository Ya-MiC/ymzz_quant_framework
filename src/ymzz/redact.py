"""把環境變數裡的密鑰從文字裡拿掉。不要記錄密鑰本身。"""

from __future__ import annotations

import os

_SECRET_ENV = ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "TELEGRAM_BOT_TOKEN")


def redact(text: str | None) -> str:
    if not text:
        return ""
    cleaned = str(text)
    for name in _SECRET_ENV:
        value = os.environ.get(name) or ""
        if len(value) >= 4 and value in cleaned:
            cleaned = cleaned.replace(value, "[redacted]")
    return cleaned
