"""單例紙上／實盤迴圈。狀態寫在磁碟，重啟後沿用同一份 state。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ymzz.errors import ConfigError, YmzzError
from ymzz.redact import redact
from ymzz.timeutil import iso_kst, now_utc


class InstanceLock:
    """同一台機器同時只跑一個 runner。程序結束會放開鎖。"""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            self.handle = None
            raise ConfigError("已有一個 ymzz runner 在跑。這次沒有啟動第二個。") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def next_daily_check(moment: datetime) -> datetime:
    """下一個 UTC 00:10。日線收盤後留十分鐘再看。"""
    utc = moment.astimezone(timezone.utc)
    slot = utc.replace(hour=0, minute=10, second=0, microsecond=0)
    if slot <= utc:
        slot += timedelta(days=1)
    return slot


def write_heartbeat(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run_cycles(*, args, step, sleep, lock_path: Path, heartbeat_path: Path, cycles: int | None, clock=None) -> dict:
    """每輪寫心跳，並對最新已收盤 K 線做一次決策。同一根 K 線由 paper 保證不重複下單。"""
    clock = clock or now_utc
    last = {}
    with InstanceLock(lock_path):
        count = 0
        while True:
            moment = clock()
            beat = {
                "pid": os.getpid(),
                "mode": args.mode,
                "beat_utc": moment.astimezone(timezone.utc).isoformat(),
                "beat_kst": iso_kst(moment),
                "interval_seconds": args.interval,
                "next_heartbeat_utc": (moment + timedelta(seconds=args.interval)).astimezone(timezone.utc).isoformat(),
                "next_candle_check_utc": next_daily_check(moment).isoformat(),
                "last_error": None,
                "last_action": None,
                "idempotent": None,
            }
            try:
                decision = step(args)
                beat["last_action"] = decision.get("action")
                beat["idempotent"] = bool(decision.get("idempotent"))
                beat["bar_open"] = decision.get("bar_open")
                beat["reason"] = decision.get("reason")
            except YmzzError as exc:
                beat["last_error"] = redact(exc.message_zh)[:300]
            except Exception as exc:
                beat["last_error"] = redact(str(exc))[:300]
            write_heartbeat(heartbeat_path, beat)
            last = beat
            count += 1
            if cycles is not None and count >= cycles:
                break
            sleep(args.interval)
    return last
