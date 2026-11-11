"""一筆紙上決策。同一根已收盤 K 線重跑不會再下單。

紙上單透過 kraken-cli（MIT，krakenfx/kraken-cli）的
`kraken paper buy` / `kraken paper sell` / `kraken paper status`。
本模組不讀取、不印出 API 密鑰。實盤路徑另有硬性上限。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ymzz.engine import floor_qty
from ymzz.errors import ConfigError
from ymzz.timeutil import KST, iso_kst


class LiveRefused(ConfigError):
    pass


def guard_live(*, live: bool, confirm: bool, capital: float, leverage: float, instrument: str) -> None:
    """實盤必須同時具備環境變數與確認旗標，而且本金、槓桿、止損有硬上限。"""
    if not live:
        return
    key_set = bool(os.environ.get("KRAKEN_API_KEY"))
    secret_set = bool(os.environ.get("KRAKEN_API_SECRET"))
    if not key_set or not secret_set:
        missing = []
        if not key_set:
            missing.append("KRAKEN_API_KEY")
        if not secret_set:
            missing.append("KRAKEN_API_SECRET")
        raise LiveRefused("實盤拒絕：環境變數未設定：" + "、".join(missing) + "。程式不會印出變數內容。")
    if not confirm:
        raise LiveRefused("實盤拒絕：必須同時加上 --i-confirm-live。這次沒有送出任何單。")
    if capital - 10 > 1e-9:
        raise LiveRefused("實盤拒絕：本金上限 10 美元。")
    if instrument != "spot" or abs(leverage - 1.0) > 1e-9:
        raise LiveRefused("實盤拒絕：只允許現貨 1 倍。")


def kraken_pair(symbol: str) -> str:
    text = symbol.upper().replace("/", "").replace("-", "")
    if text in {"BTCUSD", "XBTUSD"}:
        return "BTCUSD"
    raise ConfigError("紙上與實盤路徑這版只接受 BTC/USD。不會把其他代碼猜成 Kraken 交易對。")


def order_commands(
    *,
    live: bool,
    side: str,
    pair: str,
    qty: float,
    reason: str,
    stop_price: float | None,
) -> list[list[str]]:
    """回傳要執行的 argv。實盤買單後面一定帶一張止損賣單。"""
    if side not in {"buy", "sell"}:
        raise ConfigError("方向只能是 buy 或 sell")
    qty_text = f"{qty:.8f}"
    if live:
        if side == "buy":
            if stop_price is None or stop_price <= 0:
                raise LiveRefused("實盤拒絕：買單沒有止損價。")
            return [
                ["kraken", "order", "buy", pair, qty_text, "--type", "market", "--reason", reason],
                [
                    "kraken",
                    "order",
                    "sell",
                    pair,
                    qty_text,
                    "--type",
                    "stop-loss",
                    "--price",
                    f"{stop_price:.1f}",
                    "--reason",
                    "硬止損，隨買單附上",
                ],
            ]
        return [["kraken", "order", "sell", pair, qty_text, "--type", "market", "--reason", reason]]
    commands = [["kraken", "paper", side, pair, qty_text, "--type", "market", "--reason", reason]]
    commands.append(["kraken", "paper", "status"])
    return commands


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_logs(csv_path: Path, md_path: Path, row: dict) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["time_kst", "bar_open", "rsi", "dif", "dea", "action", "reason", "qty", "price"]
    if not csv_path.exists():
        csv_path.write_text(",".join(fields) + "\n", encoding="utf-8")
    values = []
    for key in fields:
        text = "" if row.get(key) is None else str(row[key]).replace(",", " ")
        values.append(text)
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write(",".join(values) + "\n")
    if not md_path.exists():
        md_path.write_text("# 紙上決策紀錄\n\n", encoding="utf-8")
    line = (
        f"- {row['time_kst']}  K 線 {row['bar_open']}  {row['action']}  {row['reason']}"
        f"  RSI {row.get('rsi')}  DIF {row.get('dif')}  DEA {row.get('dea')}  數量 {row.get('qty')}\n"
    )
    with md_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def step_decision(
    *,
    bars,
    signals: list[float],
    indicator_exits: list[float],
    indicators: dict,
    state_path: Path,
    csv_path: Path,
    md_path: Path,
    capital: float,
    position_frac: float,
    sl_pct: float,
    tp_pct: float,
    daily_loss_halt_pct: float | None,
    ordermin: float,
    costmin: float,
    lot_decimals: int,
    symbol: str,
    live: bool,
    confirm: bool,
    leverage: float,
    runner,
    now: datetime | None = None,
) -> dict:
    """對最新一根已收盤 K 線做一次決策。重跑同一根不會再送單。"""
    if not bars:
        raise ConfigError("沒有已收盤 K 線，不能做紙上決策。")
    guard_live(live=live, confirm=confirm, capital=capital, leverage=leverage, instrument="spot")
    bar = bars[-1]
    index = len(bars) - 1
    bar_open = bar.open_time.isoformat()
    state = _load_state(state_path)
    now = now or datetime.now(timezone.utc)
    if state.get("cash") is None:
        state["cash"] = capital
    if state.get("initial_capital") is None:
        state["initial_capital"] = capital
    state["sl_pct"] = sl_pct
    state["tp_pct"] = tp_pct
    state["daily_loss_halt_pct"] = daily_loss_halt_pct
    rsi = _last(indicators.get("rsi"), index)
    dif = _last(indicators.get("dif"), index)
    dea = _last(indicators.get("dea"), index)

    def finish(action: str, reason: str, qty: float = 0.0, price: float | None = None, commands: list | None = None) -> dict:
        decision = {
            "time_kst": iso_kst(now),
            "bar_open": bar_open,
            "rsi": _fmt(rsi),
            "dif": _fmt(dif),
            "dea": _fmt(dea),
            "action": action,
            "reason": reason,
            "qty": qty,
            "price": price if price is not None else bar.close,
        }
        _append_logs(csv_path, md_path, decision)
        state["last_bar_open"] = bar_open
        state["last_decision"] = decision
        _snapshot_equity(state, float(decision["price"]), now)
        _save_state(state_path, state)
        decision["commands"] = commands or []
        decision["idempotent"] = False
        return decision

    if state.get("last_bar_open") == bar_open:
        previous = dict(state.get("last_decision") or {})
        previous["idempotent"] = True
        previous["commands"] = []
        note = {
            "time_kst": iso_kst(now),
            "bar_open": bar_open,
            "rsi": previous.get("rsi", ""),
            "dif": previous.get("dif", ""),
            "dea": previous.get("dea", ""),
            "action": "repeat",
            "reason": "同一根已收盤 K 線已經決策過，不再下單",
            "qty": previous.get("qty", 0),
            "price": previous.get("price", bar.close),
        }
        _append_logs(csv_path, md_path, note)
        return previous

    qty_held = float(state.get("qty") or 0.0)
    entry_fill = float(state.get("entry_fill") or 0.0)
    day = bar.open_time.astimezone(timezone.utc).date().isoformat()
    if state.get("sod_date") != day:
        state["sod_date"] = day
        state["sod_equity"] = float(state.get("cash") if state.get("cash") is not None else capital)
        state["halted"] = False
    cash = float(state.get("cash") if state.get("cash") is not None else capital)
    equity = cash + qty_held * bar.close
    if (
        daily_loss_halt_pct
        and state.get("sod_equity")
        and (float(state["sod_equity"]) - equity) / float(state["sod_equity"]) >= daily_loss_halt_pct - 1e-15
    ):
        state["halted"] = True

    exit_reason = None
    if qty_held > 0 and entry_fill > 0:
        sl = entry_fill * (1.0 - sl_pct)
        tp = entry_fill * (1.0 + tp_pct)
        if bar.open <= sl:
            exit_reason = "stop_loss"
        elif bar.open >= tp:
            exit_reason = "take_profit"
        elif bar.low <= sl and bar.high >= tp:
            exit_reason = "stop_loss"
        elif bar.low <= sl:
            exit_reason = "stop_loss"
        elif bar.high >= tp:
            exit_reason = "take_profit"
        elif float(indicator_exits[index] or 0) > 0:
            exit_reason = "indicator_exit"

    pair = kraken_pair(symbol)
    if exit_reason:
        commands = order_commands(live=live, side="sell", pair=pair, qty=qty_held, reason=exit_reason, stop_price=None)
        results = _run_all(commands, runner)
        fill = python_fill_from_results(results)
        exit_price = bar.close
        if fill:
            exit_price = float(fill["fill_price"])
            fee = float(fill.get("fee") or 0.0)
            state["cash"] = cash + qty_held * exit_price - fee
            _remember_fill(state, fill)
        else:
            state["cash"] = cash + qty_held * exit_price
        state["qty"] = 0.0
        state["entry_fill"] = 0.0
        state["stop_price"] = None
        return finish(exit_reason, "已持倉，這根已收盤 K 線觸發出場", qty_held, exit_price, commands)

    want_entry = qty_held <= 0 and float(signals[index] or 0) > 0
    if want_entry and state.get("halted"):
        return finish("skip", "daily_circuit_breaker", 0.0, bar.close, [])
    if want_entry:
        price = bar.close
        budget = equity * position_frac
        raw_qty = budget / price if price > 0 else 0.0
        qty = floor_qty(raw_qty, lot_decimals)
        notional = qty * price
        if qty <= 0 or qty < ordermin or notional < costmin:
            return finish(
                "skip",
                "below minimum",
                qty,
                price,
                [],
            )
        stop = price * (1.0 - sl_pct)
        reason = f"收盤進場訊號 RSI={_fmt(rsi)} DIF={_fmt(dif)} DEA={_fmt(dea)}"
        commands = order_commands(live=live, side="buy", pair=pair, qty=qty, reason=reason, stop_price=stop)
        results = _run_all(commands, runner)
        fill = python_fill_from_results(results)
        if fill:
            price = float(fill["fill_price"])
            fee = float(fill.get("fee") or 0.0)
            notional = qty * price
            stop = price * (1.0 - sl_pct)
            state["cash"] = cash - notional - fee
            _remember_fill(state, fill)
        else:
            state["cash"] = cash - notional
        state["qty"] = qty
        state["entry_fill"] = price
        state["stop_price"] = stop
        return finish("buy", reason, qty, price, commands)

    return finish("hold", "這根已收盤 K 線沒有新的進出場", qty_held, bar.close, [])


def _remember_fill(state: dict, fill: dict) -> None:
    rows = list(state.get("sim_fills") or [])
    rows.append(
        {
            "side": fill.get("side"),
            "qty": fill.get("qty"),
            "ticker": fill.get("ticker"),
            "fill_price": fill.get("fill_price"),
            "fee": fill.get("fee"),
            "slip_bp": fill.get("slip_bp"),
            "fee_bp": fill.get("fee_bp"),
        }
    )
    state["sim_fills"] = rows[-500:]


def _last(values, index):
    if not values or index >= len(values):
        return None
    value = values[index]
    return value


def _run_all(commands: list[list[str]], runner) -> list:
    results = []
    for argv in commands:
        result = runner(argv)
        code = getattr(result, "returncode", 0)
        if code not in (0, None):
            err = getattr(result, "stderr", "") or ""
            raise ConfigError(f"Kraken 指令失敗（結束碼 {code}）：{' '.join(argv[:4])} {err[:180]}")
        results.append(result)
    return results


def python_fill_from_results(results: list) -> dict | None:
    """純 Python 紙上成交會在 stdout 放 engine=ymzz-python。kraken-cli 的輸出不採用。"""
    for result in results:
        raw = getattr(result, "stdout", "") or ""
        if not str(raw).strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("engine") == "ymzz-python" and payload.get("fill_price") is not None:
            return payload
    return None


def _snapshot_equity(state: dict, mark: float, now: datetime) -> None:
    cash = float(state.get("cash") or 0.0)
    qty = float(state.get("qty") or 0.0)
    equity = cash + qty * float(mark)
    peak = max(float(state.get("equity_peak") or equity), equity)
    state["equity_peak"] = peak
    state["equity"] = equity
    state["drawdown"] = (peak - equity) / peak if peak > 0 else 0.0
    curve = list(state.get("equity_curve") or [])
    curve.append(
        {
            "time_kst": iso_kst(now),
            "equity": equity,
            "drawdown": state["drawdown"],
            "price": float(mark),
        }
    )
    state["equity_curve"] = curve[-2000:]
