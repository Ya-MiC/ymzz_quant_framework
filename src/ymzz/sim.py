"""沒有 kraken 執行檔時的紙上成交。

用公開行情的最後成交價，加上與回測相同的手續費（bp）與滑點（bp）。
不讀 API 密鑰，也不會把這條路徑當成實盤單。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from ymzz.errors import ConfigError


def simulate_fill(*, side: str, qty: float, ticker: float, fee_bp: float, slip_bp: float) -> dict:
    if side not in {"buy", "sell"}:
        raise ConfigError("紙上成交方向只能是 buy 或 sell")
    if qty <= 0 or ticker <= 0:
        raise ConfigError("紙上成交需要正的數量與公開成交價")
    if fee_bp < 0 or slip_bp < 0:
        raise ConfigError("手續費與滑點不能為負")
    slip = slip_bp / 10_000.0
    fill = ticker * (1.0 + slip) if side == "buy" else ticker * (1.0 - slip)
    fee = qty * fill * fee_bp / 10_000.0
    return {
        "engine": "ymzz-python",
        "side": side,
        "qty": qty,
        "ticker": ticker,
        "fill_price": fill,
        "fee": fee,
        "fee_bp": fee_bp,
        "slip_bp": slip_bp,
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


def public_ticker(pair: str = "XBTUSD", getter=None) -> float:
    """Kraken 公開 Ticker 的最後成交價。失敗就停，不編一個價。"""
    if getter is None:
        response = httpx.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": pair},
            timeout=20.0,
            headers={"User-Agent": "YMZZResearch/0.1 (local paper; keyless)"},
        )
        response.raise_for_status()
        payload = response.json()
    else:
        payload = getter(pair)
    if payload.get("error"):
        raise ConfigError(f"公開 Ticker 拒絕報價：{payload['error']}")
    result = payload.get("result") or {}
    if not result:
        raise ConfigError("公開 Ticker 沒有結果，不能用紙上價代替。")
    book = next(iter(result.values()))
    last = (book.get("c") or [None])[0]
    if last in (None, ""):
        raise ConfigError("公開 Ticker 沒有最後成交價。")
    return float(last)


def _result(code: int, payload: dict | None = None, err: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout="" if payload is None else json.dumps(payload), stderr=err)


def python_paper_command(argv: list[str], *, ticker: float, fee_bp: float, slip_bp: float, ledger_path: Path) -> SimpleNamespace:
    """解釋 `kraken paper buy|sell|status`。其他子命令不在這裡成交。"""
    if len(argv) < 3 or argv[0] != "kraken" or argv[1] != "paper":
        return _result(127, err="這不是紙上指令。")
    action = argv[2]
    if action == "status":
        rows = []
        if ledger_path.exists():
            for line in ledger_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return _result(0, {"engine": "ymzz-python", "fills": rows[-50:]})
    if action not in {"buy", "sell"} or len(argv) < 5:
        return _result(2, err="紙上指令格式無法辨認。")
    qty = float(argv[4])
    fill = simulate_fill(side=action, qty=qty, ticker=ticker, fee_bp=fee_bp, slip_bp=slip_bp)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(fill, ensure_ascii=False) + "\n")
    return _result(0, fill)


def build_runner(*, fee_bp: float, slip_bp: float, ledger_path: Path, ticker_fn=None, which=None, pair: str = "XBTUSD"):
    """有 kraken 執行檔就交給它。沒有時，紙上單改走純 Python；實盤單直接失敗。"""
    finder = which or shutil.which

    def runner(argv: list[str]):
        binary = finder("kraken")
        paper = len(argv) >= 2 and argv[0] == "kraken" and argv[1] == "paper"
        live_order = len(argv) >= 2 and argv[0] == "kraken" and argv[1] == "order"
        if binary and not paper:
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        if binary and paper:
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        if paper:
            if argv[2:3] == ["status"]:
                return python_paper_command(argv, ticker=1.0, fee_bp=fee_bp, slip_bp=slip_bp, ledger_path=ledger_path)
            price = ticker_fn() if ticker_fn else public_ticker(pair)
            return python_paper_command(argv, ticker=price, fee_bp=fee_bp, slip_bp=slip_bp, ledger_path=ledger_path)
        if live_order:
            return _result(127, err="找不到 kraken 執行檔。實盤單不會改用純 Python 成交。")
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    return runner
