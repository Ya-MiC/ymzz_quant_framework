"""時間一律以 UTC 儲存，報告另給首爾時間。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
CST = ZoneInfo("Asia/Shanghai")


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_kst(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(KST)


def iso_utc(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def iso_kst(dt: datetime) -> str:
    return to_kst(dt).isoformat()


def stamp_pair(dt: datetime | None = None) -> dict[str, str]:
    dt = now_utc() if dt is None else ensure_utc(dt)
    return {"fetched_at_utc": iso_utc(dt), "fetched_at_kst": iso_kst(dt)}
