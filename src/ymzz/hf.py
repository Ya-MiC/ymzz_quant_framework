"""一個月高頻觸發。規則在執行時是固定的，不再搜參數，也不呼叫模型。

訓練只用較早的 K 線。最近 30 日只在參數凍結之後評分。
紙上預設是 Kraken 現貨 1 倍、每筆名義 10 美元。空單只記在紙上帳。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from ymzz.engine import Bar, Funding
from ymzz.errors import ConfigError
from ymzz.indicators import rsi
from ymzz.paper import LiveRefused

# 現貨第一級：做市 0.40%、吃單 0.80%。見入金教程引用的 Kraken 費率說明。
SPOT_MAKER_BPS = 40.0
SPOT_TAKER_BPS = 80.0
SLIP_BPS = 5.0
# feeschedules 裡 Incentive Rebate MTF Linear 的第一級是 makerFee 0.02、takerFee 0.05。
# 旁邊的負費率是 -0.01 這種回扣，所以這兩個數是百分數：0.02% = 2 bp，0.05% = 5 bp。
FUTURES_MAKER_BPS = 2.0
FUTURES_TAKER_BPS = 5.0

# 下面這組數字只會在訓練窗挑完之後改一次。樣本外的結果不用來換一組更好看的參數。
PRESET = {
    "period": 14,
    "oversold": 30.0,
    "overbought": 70.0,
    "expire": 4,
    "sl": 0.008,
    "tp": 0.025,
    "maker_bps": SPOT_MAKER_BPS,
    "taker_bps": SPOT_TAKER_BPS,
    "slip_bps": SLIP_BPS,
    "leverage": 1.0,
    "capital": 10.0,
    "max_margin_usd": 10.0,
    "max_notional_usd": 10.0,
    "daily_loss": 0.15,
    "global_dd": 0.15,
    "symbol": "BTC/USD",
    "timeframe": "1h",
}

GRID: list[dict] = []
for _period in (7, 14):
    for _low, _high in ((30.0, 70.0), (35.0, 65.0)):
        for _sl in (0.008, 0.015):
            for _tp in (0.015, 0.025):
                for _expire in (2, 4):
                    GRID.append(
                        {
                            "period": _period,
                            "oversold": _low,
                            "overbought": _high,
                            "sl": _sl,
                            "tp": _tp,
                            "expire": _expire,
                        }
                    )


def trigger_signals(closes, period: int, oversold: float, overbought: float) -> list[float]:
    """收盤才知道。上穿超賣做多，下穿超買做空。同一根只會有一個方向。"""
    if not (0 < float(oversold) < float(overbought) < 100):
        raise ConfigError("超賣線要低於超買線，而且都在 0 到 100 之間")
    line = rsi(closes, int(period))
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        prev, cur = float(line[i - 1]), float(line[i])
        if not (math.isfinite(prev) and math.isfinite(cur)):
            continue
        if prev < oversold <= cur:
            out[i] = 1.0
        elif prev > overbought >= cur:
            out[i] = -1.0
    return out


@dataclass
class HfConfig:
    capital: float = 10.0
    leverage: float = 1.0
    max_margin_usd: float = 10.0
    max_notional_usd: float = 10.0
    maker_bps: float = SPOT_MAKER_BPS
    taker_bps: float = SPOT_TAKER_BPS
    slip_bps: float = SLIP_BPS
    sl: float = 0.015
    tp: float = 0.025
    expire: int = 4
    daily_loss: float = 0.15
    global_dd: float = 0.15
    entry: str = "maker"
    lot_decimals: int | None = None
    ordermin: float = 0.0
    costmin: float = 0.0
    store_equity: bool = True

    def validated(self) -> HfConfig:
        if self.capital <= 0:
            raise ConfigError("本金要大於 0")
        if not (0 < self.leverage <= 2 + 1e-12):
            raise ConfigError("槓桿要大於 0，而且不大於 2")
        if self.sl <= 0 or self.tp <= 0:
            raise ConfigError("止損和止盈都要大於 0。這一策略進場就附上，不能事後再補。")
        if int(self.expire) < 1:
            raise ConfigError("限價單至少留 1 根 K")
        if self.entry not in {"maker", "taker"}:
            raise ConfigError("進場只接受 maker 或 taker")
        if self.max_margin_usd <= 0 or self.max_notional_usd <= 0:
            raise ConfigError("保證金上限和名義上限都要大於 0")
        self.expire = int(self.expire)
        return self


def fresh_state(capital: float) -> dict:
    return {
        "cash": float(capital),
        "qty": 0.0,
        "entry": 0.0,
        "entry_fee": 0.0,
        "entry_time": None,
        "stop": 0.0,
        "take": 0.0,
        "limit": None,
        "pending_taker": 0,
        "bar_index": -1,
        "last_bar_open": None,
        "sod_key": None,
        "sod_equity": float(capital),
        "day_halt": False,
        "peak": float(capital),
        "global_halt": False,
        "fees_paid": 0.0,
        "fees_maker": 0.0,
        "fees_taker": 0.0,
        "fees_funding": 0.0,
        "missing_funding": 0,
        "round_trips": [],
        "min_equity": float(capital),
        "max_drawdown": 0.0,
        "entries": 0,
        "missed_limits": 0,
        "stops": 0,
        "takes": 0,
        "skipped_lot": 0,
        "halt_skips": 0,
        "max_entry_notional": 0.0,
    }


def equity_at(state: dict, price: float) -> float:
    qty = state["qty"]
    if qty == 0:
        return float(state["cash"])
    return float(state["cash"]) + qty * (price - state["entry"])


def _fee(notional: float, bps: float) -> float:
    return abs(notional) * bps / 10000.0


def _worsen(price: float, side: int, bps: float) -> float:
    if side > 0:
        return price * (1.0 - bps / 10000.0)
    return price * (1.0 + bps / 10000.0)


def _qty_for(equity: float, price: float, cfg: HfConfig) -> float:
    notional = min(equity * cfg.leverage, cfg.max_margin_usd * cfg.leverage, cfg.max_notional_usd)
    if notional <= 0 or price <= 0:
        return 0.0
    raw = notional / price
    if cfg.lot_decimals is not None:
        scale = 10 ** int(cfg.lot_decimals)
        raw = math.floor(raw * scale + 1e-12) / scale
    if raw <= 0 or raw + 1e-15 < cfg.ordermin or raw * price + 1e-9 < cfg.costmin:
        return 0.0
    return raw


def _charge(state: dict, fee: float, maker: bool) -> None:
    state["cash"] -= fee
    state["fees_paid"] += fee
    if maker:
        state["fees_maker"] += fee
    else:
        state["fees_taker"] += fee


def _open_position(state: dict, side: int, fill: float, cfg: HfConfig, maker: bool) -> bool:
    qty_abs = _qty_for(equity_at(state, fill), fill, cfg)
    if qty_abs <= 0:
        state["skipped_lot"] += 1
        state["limit"] = None
        state["pending_taker"] = 0
        return False
    fee = _fee(qty_abs * fill, cfg.maker_bps if maker else cfg.taker_bps)
    _charge(state, fee, maker)
    state["qty"] = qty_abs if side > 0 else -qty_abs
    state["entry"] = fill
    state["entry_fee"] = fee
    if side > 0:
        state["stop"] = fill * (1.0 - cfg.sl)
        state["take"] = fill * (1.0 + cfg.tp)
    else:
        state["stop"] = fill * (1.0 + cfg.sl)
        state["take"] = fill * (1.0 - cfg.tp)
    state["entries"] += 1
    state["max_entry_notional"] = max(state["max_entry_notional"], qty_abs * fill)
    state["limit"] = None
    state["pending_taker"] = 0
    return True


def _close_position(state: dict, exit_price: float, cfg: HfConfig, bar: Bar, maker: bool, reason: str) -> None:
    qty = state["qty"]
    if qty == 0:
        return
    fee = _fee(abs(qty) * exit_price, cfg.maker_bps if maker else cfg.taker_bps)
    price_pnl = qty * (exit_price - state["entry"])
    state["cash"] += price_pnl
    _charge(state, fee, maker)
    pnl = price_pnl - state["entry_fee"] - fee
    if state["cash"] < 0:
        state["cash"] = 0.0
        state["global_halt"] = True
    state["round_trips"].append(
        {
            "entry_time": state.get("entry_time"),
            "exit_time": bar.close_time.isoformat(),
            "side": "long" if qty > 0 else "short",
            "reason": reason,
            "qty": qty,
            "entry": state["entry"],
            "exit": exit_price,
            "pnl": pnl,
            "fees": state["entry_fee"] + fee,
            "equity_after": state["cash"],
        }
    )
    if reason == "stop":
        state["stops"] += 1
    elif reason == "take":
        state["takes"] += 1
    state["qty"] = 0.0
    state["entry"] = 0.0
    state["entry_fee"] = 0.0
    state["entry_time"] = None
    state["stop"] = 0.0
    state["take"] = 0.0


def _exit_on_bar(state: dict, bar: Bar, cfg: HfConfig, allow_take: bool) -> bool:
    qty = state["qty"]
    if qty == 0:
        return False
    if qty > 0:
        if bar.open <= state["stop"]:
            _close_position(state, _worsen(bar.open, 1, cfg.slip_bps), cfg, bar, False, "stop")
            return True
        if bar.low <= state["stop"]:
            _close_position(state, _worsen(state["stop"], 1, cfg.slip_bps), cfg, bar, False, "stop")
            return True
        if allow_take and bar.high > state["take"]:
            _close_position(state, state["take"], cfg, bar, True, "take")
            return True
        return False
    if bar.open >= state["stop"]:
        _close_position(state, _worsen(bar.open, -1, cfg.slip_bps), cfg, bar, False, "stop")
        return True
    if bar.high >= state["stop"]:
        _close_position(state, _worsen(state["stop"], -1, cfg.slip_bps), cfg, bar, False, "stop")
        return True
    if allow_take and bar.low < state["take"]:
        _close_position(state, state["take"], cfg, bar, True, "take")
        return True
    return False


def _try_maker_fill(state: dict, bar: Bar, cfg: HfConfig) -> None:
    limit = state["limit"]
    if not limit:
        return
    index = state["bar_index"]
    if index <= limit["born"] or index > limit["born"] + cfg.expire:
        return
    side = int(limit["side"])
    price = float(limit["price"])
    if side > 0:
        traded = bar.open > price and bar.low < price
    else:
        traded = bar.open < price and bar.high > price
    if not traded:
        return
    if not _open_position(state, side, price, cfg, True):
        return
    state["entry_time"] = bar.open_time.isoformat()
    # 同一根能穿過限價，不假定高點發生在成交之後，所以這根只允許打止損。
    _exit_on_bar(state, bar, cfg, allow_take=False)


def step_bar(state: dict, bar: Bar, signal: float, cfg: HfConfig, funding_events: list[Funding] | None = None) -> float:
    """一根已收盤 K。先處理舊單，收盤才允許掛新的限價。"""
    cfg = cfg.validated()
    state["bar_index"] += 1
    state["last_bar_open"] = bar.open_time.isoformat()
    qty_into_bar = state["qty"]
    eq_open = equity_at(state, bar.open)
    day = bar.open_time.astimezone(timezone.utc).date().isoformat()
    if state["sod_key"] != day:
        state["sod_key"] = day
        state["sod_equity"] = eq_open
        state["day_halt"] = False
    if state["sod_equity"] > 0 and (state["sod_equity"] - eq_open) / state["sod_equity"] >= cfg.daily_loss - 1e-15:
        state["day_halt"] = True
    if state["peak"] > 0 and (state["peak"] - eq_open) / state["peak"] >= cfg.global_dd - 1e-15:
        state["global_halt"] = True
    halted = bool(state["day_halt"] or state["global_halt"])
    if halted and state["limit"] is not None:
        state["limit"] = None
        state["missed_limits"] += 1
        state["halt_skips"] += 1
    if halted and state["pending_taker"]:
        state["pending_taker"] = 0
        state["halt_skips"] += 1

    if state["qty"] != 0:
        _exit_on_bar(state, bar, cfg, allow_take=True)

    if state["qty"] == 0 and not halted:
        if cfg.entry == "taker" and state["pending_taker"]:
            side = int(state["pending_taker"])
            raw = bar.open * (1.0 + cfg.slip_bps / 10000.0) if side > 0 else bar.open * (1.0 - cfg.slip_bps / 10000.0)
            if _open_position(state, side, raw, cfg, False):
                state["entry_time"] = bar.open_time.isoformat()
                _exit_on_bar(state, bar, cfg, allow_take=True)
            else:
                state["pending_taker"] = 0
        elif cfg.entry == "maker":
            _try_maker_fill(state, bar, cfg)

    limit = state["limit"]
    if limit is not None and state["bar_index"] >= limit["born"] + cfg.expire:
        state["limit"] = None
        state["missed_limits"] += 1

    if state["qty"] == 0 and state["limit"] is None and not state["pending_taker"] and abs(float(signal)) > 1e-12:
        if halted:
            state["halt_skips"] += 1
        else:
            side = 1 if float(signal) > 0 else -1
            if cfg.entry == "maker":
                state["limit"] = {"side": side, "price": float(bar.close), "born": state["bar_index"]}
            else:
                state["pending_taker"] = side

    funded_qty = qty_into_bar if qty_into_bar != 0 else state["qty"]
    if funding_events and funded_qty != 0:
        for event in funding_events:
            payment = funded_qty * bar.close * event.rate
            state["cash"] -= payment
            state["fees_funding"] += payment
            if state["cash"] < 0:
                state["cash"] = 0.0
                state["qty"] = 0.0
                state["global_halt"] = True
                break
    elif funding_events is not None and not funding_events and funded_qty != 0:
        state["missing_funding"] += 1

    eq = equity_at(state, bar.close)
    if eq < 0:
        state["cash"] = 0.0
        state["qty"] = 0.0
        state["global_halt"] = True
        eq = 0.0
    if eq < state["min_equity"]:
        state["min_equity"] = eq
    if eq > state["peak"]:
        state["peak"] = eq
    if state["peak"] > 0:
        state["max_drawdown"] = max(state["max_drawdown"], (state["peak"] - eq) / state["peak"])
    return eq


def _events_for(bar: Bar, funding: list[Funding], cursor: int) -> tuple[list[Funding], int]:
    while cursor < len(funding) and funding[cursor].time < bar.open_time:
        cursor += 1
    start = cursor
    events = []
    while cursor < len(funding) and funding[cursor].time < bar.close_time:
        if bar.open_time <= funding[cursor].time < bar.close_time:
            events.append(funding[cursor])
        cursor += 1
    if start == cursor and not events:
        return events, cursor
    return events, cursor


def run_hf(bars: list[Bar], signals: list[float], cfg: HfConfig, funding: list[Funding] | None = None) -> dict:
    cfg = cfg.validated()
    if len(signals) != len(bars):
        raise ConfigError("訊號根數和 K 線不同")
    state = fresh_state(cfg.capital)
    curve = []
    cursor = 0
    series = funding or []
    charge_funding = funding is not None
    for i, bar in enumerate(bars):
        events = None
        if charge_funding:
            events, cursor = _events_for(bar, series, cursor)
        eq = step_bar(state, bar, signals[i], cfg, events if charge_funding else None)
        if cfg.store_equity:
            curve.append({"time": bar.close_time.isoformat(), "equity": eq})
    trips = state["round_trips"]
    wins = sum(1 for row in trips if row["pnl"] > 0)
    final = curve[-1]["equity"] if curve else equity_at(state, bars[-1].close if bars else 0.0)
    if not cfg.store_equity and bars:
        final = equity_at(state, bars[-1].close)
    return {
        "capital": cfg.capital,
        "final_equity": final,
        "min_equity": state["min_equity"],
        "total_return": (final / cfg.capital - 1.0) if cfg.capital else None,
        "max_drawdown": state["max_drawdown"],
        "round_trips": len(trips),
        "wins": wins,
        "win_rate": (wins / len(trips)) if trips else None,
        "fees_paid": state["fees_paid"],
        "fees_maker": state["fees_maker"],
        "fees_taker": state["fees_taker"],
        "fees_funding": state["fees_funding"],
        "missing_funding": state["missing_funding"],
        "entries": state["entries"],
        "missed_limits": state["missed_limits"],
        "stops": state["stops"],
        "takes": state["takes"],
        "skipped_lot": state["skipped_lot"],
        "halt_skips": state["halt_skips"],
        "global_halt": state["global_halt"],
        "open_qty": state["qty"],
        "max_entry_notional": state["max_entry_notional"],
        "equity": curve,
        "trades": trips,
    }


def choose_hf(rows: list[dict]) -> tuple[dict, str]:
    """只看訓練窗。先留住本金、再取回撤最小。沒有守住本金就明說，仍凍結虧最少的那組。"""

    def calm(row: dict) -> tuple:
        return (row["max_drawdown"], -row["final_equity"], -row["round_trips"])

    kept = [
        row
        for row in rows
        if row["round_trips"] >= 5 and row["min_equity"] >= 0.5 and row["final_equity"] + 1e-9 >= row["capital"]
    ]
    if kept:
        kept.sort(key=calm)
        return kept[0], "train_preserved"
    alive = [row for row in rows if row["round_trips"] >= 5 and row["min_equity"] >= 0.5]
    if alive:
        alive.sort(key=lambda row: (-row["final_equity"], row["max_drawdown"]))
        return alive[0], "train_lost"
    ordered = sorted(rows, key=lambda row: (-row["final_equity"], row["max_drawdown"]))
    return ordered[0], "train_unusable"


def preset_config(**overrides) -> HfConfig:
    raw = dict(PRESET)
    raw.update(overrides)
    return HfConfig(
        capital=float(raw["capital"]),
        leverage=float(raw["leverage"]),
        max_margin_usd=float(raw["max_margin_usd"]),
        max_notional_usd=float(raw["max_notional_usd"]),
        maker_bps=float(raw["maker_bps"]),
        taker_bps=float(raw["taker_bps"]),
        slip_bps=float(raw["slip_bps"]),
        sl=float(raw["sl"]),
        tp=float(raw["tp"]),
        expire=int(raw["expire"]),
        daily_loss=float(raw["daily_loss"]),
        global_dd=float(raw["global_dd"]),
        entry=str(raw.get("entry", "maker")),
        lot_decimals=raw.get("lot_decimals"),
        ordermin=float(raw.get("ordermin") or 0.0),
        costmin=float(raw.get("costmin") or 0.0),
    )


def apply_hf_preset(args) -> None:
    """讓現有的 paper / run 指令帶上這組凍結參數。"""
    args.strategy = "hf_trigger"
    args.source = "kraken"
    args.symbol = PRESET["symbol"]
    args.timeframe = PRESET["timeframe"]
    args.rsi = PRESET["period"]
    args.rsi_low = PRESET["oversold"]
    args.rsi_high = PRESET["overbought"]
    args.sl = PRESET["sl"]
    args.tp = PRESET["tp"]
    args.size = 1.0
    args.capital = PRESET["capital"]
    args.fee_bp = PRESET["maker_bps"]
    args.slip_bp = PRESET["slip_bps"]
    args.daily_loss = PRESET["daily_loss"]
    if not getattr(args, "macd", None):
        args.macd = "12,26,9"
    if hasattr(args, "start"):
        args.start = None
    if hasattr(args, "state") and not getattr(args, "state", None):
        args.state = "data/paper/hf_state.json"
    if hasattr(args, "interval") and getattr(args, "interval", None) == 180:
        args.interval = 300


def _decision(state: dict, before_qty: float, before_limit: bool, bar: Bar) -> dict:
    action = "hold"
    reason = "這根沒有新成交。"
    if before_qty == 0 and state["qty"] > 0:
        action = "long"
        reason = "限價成交，止損和止盈已經附在成交價上。這是紙上帳，沒有送到 Kraken。"
    elif before_qty == 0 and state["qty"] < 0:
        action = "short"
        reason = "空單只記在紙上帳。現貨沒有借幣，也沒有送出賣單。"
    elif before_qty != 0 and state["qty"] == 0 and state["round_trips"]:
        action = "flat"
        reason = "出場：" + state["round_trips"][-1]["reason"]
    elif state["limit"] and not before_limit:
        action = "arm"
        side = "多" if state["limit"]["side"] > 0 else "空"
        reason = f"收盤觸發，掛出{side}單限價。下一根才可能成交。"
    elif state["global_halt"]:
        action = "halt"
        reason = "總回撤到了上限，不再開新倉。已經持有的倉仍按止損止盈走。"
    elif state["day_halt"]:
        action = "halt"
        reason = "今日虧損到了上限，今天不再開新倉。"
    return {
        "action": action,
        "reason": reason,
        "idempotent": False,
        "bar_open": bar.open_time.isoformat(),
        "equity": equity_at(state, bar.close),
    }


def step_hf(*, bars: list[Bar], signals: list[float], state_path: Path, cfg: HfConfig, live: bool = False) -> dict:
    """現有紙上迴圈呼叫的一步。同一根 K 線重複呼叫不會再記一次。"""
    if live:
        raise LiveRefused("hf 預設只做紙上。限價、空單和 2 倍都沒有送到 Kraken。這次沒有送出。")
    cfg = cfg.validated()
    if cfg.leverage != 1:
        raise ConfigError("紙上預設是現貨 1 倍。2 倍只留在回測裡。")
    if len(signals) != len(bars) or not bars:
        raise ConfigError("紙上這一步沒有對得上的已收盤 K 線")
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    newest = bars[-1].open_time.isoformat()
    if saved and saved.get("last_bar_open") == newest:
        decision = dict(saved.get("last_decision") or {})
        decision["idempotent"] = True
        return decision
    if saved is None:
        state = fresh_state(cfg.capital)
        pending = list(range(len(bars)))
    else:
        state = saved["state"]
        last = saved.get("last_bar_open") or ""
        if newest < last:
            raise ConfigError("K 線比上次還舊。這次沒有重算倉位。")
        pending = [i for i, bar in enumerate(bars) if bar.open_time.isoformat() > last]
    decision = {
        "action": "idle",
        "reason": "沒有新的已收盤 K 線",
        "idempotent": False,
        "bar_open": newest,
        "equity": equity_at(state, bars[-1].close),
    }
    for i in pending:
        before_qty = state["qty"]
        before_limit = state["limit"] is not None
        step_bar(state, bars[i], signals[i], cfg, None)
        decision = _decision(state, before_qty, before_limit, bars[i])
    path.write_text(
        json.dumps({"state": state, "last_bar_open": state["last_bar_open"], "last_decision": decision}, ensure_ascii=False),
        encoding="utf-8",
    )
    return decision


def _cfg_from(spec: dict, **overrides) -> HfConfig:
    raw = {
        "capital": 10.0,
        "leverage": 1.0,
        "max_margin_usd": 10.0,
        "max_notional_usd": 10.0,
        "maker_bps": SPOT_MAKER_BPS,
        "taker_bps": SPOT_TAKER_BPS,
        "slip_bps": SLIP_BPS,
        "daily_loss": 0.15,
        "global_dd": 0.15,
        "entry": "maker",
        "lot_decimals": 4,
        "store_equity": False,
    }
    raw.update(spec)
    raw.update(overrides)
    return HfConfig(
        capital=raw["capital"],
        leverage=raw["leverage"],
        max_margin_usd=raw["max_margin_usd"],
        max_notional_usd=raw["max_notional_usd"],
        maker_bps=raw["maker_bps"],
        taker_bps=raw["taker_bps"],
        slip_bps=raw["slip_bps"],
        sl=raw["sl"],
        tp=raw["tp"],
        expire=raw["expire"],
        daily_loss=raw["daily_loss"],
        global_dd=raw["global_dd"],
        entry=raw["entry"],
        lot_decimals=raw.get("lot_decimals"),
        ordermin=raw.get("ordermin") or 0.0,
        costmin=raw.get("costmin") or 0.0,
        store_equity=raw.get("store_equity", False),
    )


def _public_metrics(row: dict) -> dict:
    keep = (
        "capital",
        "final_equity",
        "min_equity",
        "total_return",
        "max_drawdown",
        "round_trips",
        "wins",
        "win_rate",
        "fees_paid",
        "fees_maker",
        "fees_taker",
        "fees_funding",
        "missing_funding",
        "entries",
        "missed_limits",
        "stops",
        "takes",
        "skipped_lot",
        "halt_skips",
        "global_halt",
        "open_qty",
        "max_entry_notional",
    )
    return {key: row[key] for key in keep if key in row}


def _window(bars: list[Bar]) -> dict:
    if not bars:
        return {"n": 0, "first": None, "last": None, "days": 0.0}
    span = (bars[-1].open_time - bars[0].open_time).total_seconds() / 86400.0
    return {
        "n": len(bars),
        "first": bars[0].open_time.date().isoformat(),
        "last": bars[-1].open_time.date().isoformat(),
        "days": round(span, 2),
    }


def _split(bars: list[Bar], days: int = 30) -> int:
    cutoff = bars[-1].open_time - timedelta(days=days)
    count = 0
    for bar in bars:
        if bar.open_time < cutoff:
            count += 1
        else:
            break
    return count


def _span_row(name: str, fee_label: str, bars: list[Bar], result: dict, kind: str) -> dict:
    window = _window(bars)
    return {
        "id": name,
        "name": name,
        "kind": kind,
        "fee_label": fee_label,
        "first": window["first"],
        "last": window["last"],
        "days": window["days"],
        "n": window["n"],
        **_public_metrics(result),
        "equity": result.get("equity") or [],
        "trades": result.get("trades") or [],
    }


def render_month(payload: dict, written: str) -> str:
    chosen = payload["selected_on_train"]
    train = payload["train"]
    headline = next(row for row in payload["rows"] if row["id"] == payload["headline_id"])
    lines = [
        "# 一個月高頻觸發",
        "",
        f"Written: {written} KST",
        "",
        "執行時沒有模型，也沒有再挑參數。多空都是收盤 RSI 的固定穿越。",
        "限價進場用做市費率；止損用吃單費率加滑點。止盈是停著的限價。同一根又打到止損又打到止盈時，算止損。",
        "每筆保證金最多 10 美元。現貨 1 倍時名義也是 10。2 倍只出現在回測，名義最多 20，而且沒有送出實盤。",
        "日虧 15% 和總回撤 15% 都只擋新倉，不把已經持有的倉提前砍掉。止損和止盈仍在。",
        "",
        "## 資料其實有多長",
        "",
        "Kraken 現貨公開 OHLC 大約只留 720 根。1 分鐘、5 分鐘、15 分鐘都不到 30 日，1 小時現貨剛好約 30 日，沒有更早的一段可以訓練。",
        "所以訓練和 30 日樣本外用的是 Kraken 線性永續 PF_XBTUSD 的 1 小時成交 K 線。那不是現貨。",
        "現貨 1 小時的最近 30 日另外跑了一次，費用仍是現貨做市 40 bp、吃單 80 bp。紙上預設跑的就是這一檔現貨。",
        "",
        "| venue | timeframe | bars | first | last | days |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["coverage"]:
        lines.append(
            f"| {row['venue']} | {row['timeframe']} | {row['n']} | {row.get('first') or ''} | {row.get('last') or ''} | {row.get('days')} |"
        )
    lines.extend(
        [
            "",
            "## 怎麼挑的",
            "",
            f"訓練窗是永續 1 小時裡、最後 30 日之前的那段：{train['first']} → {train['last']}，{train['days']} 日，{train['n']} 根。",
            "日期看起來和樣本外第一天相接，是因為分割用的是每根 K 的開盤時間。同一根不會同時在兩邊。",
            "格子事先寫死：RSI 7 或 14，門檻 30/70 或 35/65，止損 0.8% 或 1.5%，止盈 1.5% 或 2.5%，限價留 2 或 4 根。共 32 組。",
            "費用用現貨做市 40 bp、吃單 80 bp、止損再加 5 bp 滑點。這比期貨費率貴，因為紙上跑的是現貨。",
            "挑選只看訓練：來回至少 5 筆、權益沒有低於 0.5、期末仍至少有起始的 10 美元，然後取回撤最小的。",
            "若沒有任何一組守住 10 美元，就凍結期末權益最高、而且來回至少 5 筆的那組，並寫明訓練已經虧錢。不用樣本外的結果換組。",
            f"這次的標記是 `{payload['selection_tag']}`。",
            "",
            "## 凍結下來的規則",
            "",
            f"- RSI({chosen['period']}) 上穿 {chosen['oversold']} 做多，下穿 {chosen['overbought']} 做空。",
            f"- 限價掛在訊號那根收盤，之後 {chosen['expire']} 根內要從另一側穿過去才算做市成交。跳空穿過不算成交。",
            f"- 成交價往下 {chosen['sl'] * 100:.1f}% 是止損（吃單 + 5 bp），往上 {chosen['tp'] * 100:.1f}% 是止盈（做市）。空單相反。",
            "- 名義上限 10 美元（1 倍）。期貨數量按 4 位小數無條件捨去，捨成 0 就跳過，不進位。",
            "",
            "訓練窗這組的結果：",
            "",
            f"- 期末 {train['final_equity']:.4f} 美元，報酬 {train['total_return'] * 100:.2f}%，最大回撤 {train['max_drawdown'] * 100:.2f}%。",
            f"- 來回 {train['round_trips']} 筆，勝率 {_pct(train['win_rate'])}，手續費 {train['fees_paid']:.4f} 美元。",
            "",
            "## 最近 30 日樣本外",
            "",
            "下面每一列都用上面那組凍結參數，帳戶在樣本外第一根重新從 10 美元開始。沒有把訓練的盈虧帶進來，也沒有改參數。",
            "",
            "| 列 | 費用 | 天數 | 報酬 | 最大回撤 | 來回 | 勝率 | 手續費 | 資金費 | 期末 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"]:
        if row["kind"] != "oos":
            continue
        lines.append(
            f"| {row['name']} | {row['fee_label']} | {row['days']} | {_pct(row['total_return'])} | {_pct(row['max_drawdown'])} | "
            f"{row['round_trips']} | {_pct(row['win_rate'])} | {row['fees_paid']:.4f} | {row['fees_funding']:.4f} | {row['final_equity']:.4f} |"
        )
    lines.extend(
        [
            "",
            f"主列是 `{headline['name']}`。報酬 {_pct(headline['total_return'])}，最大回撤 {_pct(headline['max_drawdown'])}，"
            f"來回 {headline['round_trips']} 筆，勝率 {_pct(headline['win_rate'])}，手續費 {headline['fees_paid']:.4f} 美元。",
            payload["recommendation"]["reason_zh"],
            "限價進場那一列比下一列的市價進場少虧，但兩列都是負的。少虧來自少成交，不是因為這組規則在樣本外賺錢。",
            "紙上預設跑的是現貨 1 小時。那一列用的是公開 OHLC 實際有的最近約 30 日，不是從更長的現貨歷史裡切出來的。",
            "",
            "2 倍那一列用了真實的每小時 relativeFundingRate。費率開始之前的小時沒有用 0 填。缺資料的小時記在 missing_funding，沒有假裝收過。",
            "期貨費率那一列用的是 feeschedules 裡 Incentive Rebate MTF Linear 第一級（做市 0.02%、吃單 0.05%）。同一份清單裡 Consumer Linear 是做市和吃單都 0.25%，這次沒有拿來把結果做漂亮，也沒有拿來重挑參數。",
            "",
            "## 比 30 日更短的週期",
            "",
            "這些不是 30 日樣本外。參數也沒有在這些短週期上重挑。RSI 週期是「幾根 K」，換到 1 分鐘就變成幾分鐘，不是同一條 1 小時規則。",
            "",
            "| 列 | 天數 | 報酬 | 最大回撤 | 來回 | 勝率 | 手續費 | 期末 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["rows"]:
        if row["kind"] == "oos":
            continue
        lines.append(
            f"| {row['name']} | {row['days']} | {_pct(row['total_return'])} | {_pct(row['max_drawdown'])} | "
            f"{row['round_trips']} | {_pct(row['win_rate'])} | {row['fees_paid']:.4f} | {row['final_equity']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## 紙上怎麼跑",
            "",
            "```",
            "python -m ymzz.cli paper --preset hf",
            "python -m ymzz.cli run --preset hf --once",
            "```",
            "",
            "預設商品 BTC/USD、1 小時、現貨 1 倍、名義 10 美元、止損和止盈用上面凍結的數字。狀態在 `data/paper/hf_state.json`。",
            "同一根已收盤 K 線重複呼叫不會再記一次。`--live` 會拒絕，不會送出限價、空單或 2 倍。",
            "空單只存在紙上帳本。Kraken 現貨這一支沒有借幣。",
            "",
            "## 沒有做的事",
            "",
            "- 沒有把 30 日報酬年化之後拿來排名。一個月年化會把噪音放大。上面的報酬就是這 30 日的總報酬。",
            "- 沒有送出保證金或期貨實盤。帳戶所在地能不能做，看 Kraken 自己的資格頁，這裡不假設你已經開通。",
            "- 現貨保證金的隔夜費沒有公開歷史序列，所以沒有編一個固定利率。2 倍用的是永續合約和真實資金費。",
            "",
        ]
    )
    return "\n".join(lines)


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _coverage_spot(client, symbol: str, timeframe: str, now) -> dict:
    from ymzz.data import load_market

    spec = {
        "market": "crypto_spot",
        "source": "kraken",
        "symbol": symbol,
        "timeframe": timeframe,
        "start": None,
        "end": None,
        "data_mode": "live",
        "strict_range": False,
    }
    bars, _funding, _prov = load_market(spec, client, now)
    window = _window(bars)
    return {"venue": f"spot {symbol}", "timeframe": timeframe, "note_zh": "現貨公開 OHLC", **window, "bars": bars}


def _coverage_futures(client, symbol: str, timeframe: str, now) -> dict:
    from ymzz.data import FUTURES_MARKET, parse_kraken_futures_candles

    market = FUTURES_MARKET[symbol]
    payload, _prov = client.get_json(
        f"https://futures.kraken.com/api/charts/v1/trade/{market}/{timeframe}",
        None,
        mode="live",
    )
    bars, _extra = parse_kraken_futures_candles(payload, timeframe, now)
    bars = sorted(bars, key=lambda bar: bar.open_time)
    window = _window(bars)
    return {"venue": f"perp {market}", "timeframe": timeframe, "note_zh": "線性永續成交 K 線", **window, "bars": bars}


def run_month(root: Path) -> dict:
    """先挑訓練參數，再評最近 30 日。呼叫順序就是這個，不能對調。"""
    from ymzz.data import load_kraken_futures
    from ymzz.http_cache import DataClient
    from ymzz.timeutil import now_utc

    now = now_utc()
    client = DataClient(root / "data" / "cache")
    coverage = []
    for timeframe in ("1m", "5m", "15m", "1h"):
        spot = _coverage_spot(client, "BTC/USD", timeframe, now)
        coverage.append({key: spot[key] for key in ("venue", "timeframe", "n", "first", "last", "days", "note_zh")})
        fut = _coverage_futures(client, "BTC/USD", timeframe, now)
        coverage.append({key: fut[key] for key in ("venue", "timeframe", "n", "first", "last", "days", "note_zh")})

    bars, funding, meta = load_kraken_futures(client, "BTC/USD", "1h", now)
    bars = sorted(bars, key=lambda bar: bar.open_time)
    funding = sorted(funding, key=lambda item: item.time)
    if len(bars) < 48:
        raise ConfigError("永續 1 小時 K 線太短，沒有湊出訓練和 30 日樣本外。")
    train_n = _split(bars, 30)
    if train_n < 24 or train_n >= len(bars):
        raise ConfigError("這段 1 小時序列切不出訓練窗和最近 30 日。沒有把同一段又當訓練又當樣本外。")
    train_bars = bars[:train_n]
    oos_bars = bars[train_n:]
    train_closes = [bar.close for bar in train_bars]
    train_rows = []
    for spec in GRID:
        signals = trigger_signals(train_closes, spec["period"], spec["oversold"], spec["overbought"])
        result = run_hf(train_bars, signals, _cfg_from(spec, lot_decimals=4))
        train_rows.append({**spec, **_public_metrics(result)})
    chosen, tag = choose_hf(train_rows)
    # 參數在這裡凍結。下面才碰樣本外。
    full_closes = [bar.close for bar in bars]
    full_signals = trigger_signals(full_closes, chosen["period"], chosen["oversold"], chosen["overbought"])
    oos_signals = full_signals[train_n:]

    def oos_run(name, fee_label, *, leverage=1.0, entry="maker", with_funding=False, maker_bps=SPOT_MAKER_BPS, taker_bps=SPOT_TAKER_BPS, max_notional=None, store=False):
        notional = 10.0 * leverage if max_notional is None else max_notional
        result = run_hf(
            oos_bars,
            oos_signals,
            _cfg_from(
                chosen,
                leverage=leverage,
                max_margin_usd=10.0,
                max_notional_usd=notional,
                entry=entry,
                maker_bps=maker_bps,
                taker_bps=taker_bps,
                store_equity=store,
                lot_decimals=4,
            ),
            funding if with_funding else None,
        )
        cap = 10.0 * leverage
        if result["max_entry_notional"] > cap + 0.05:
            raise ConfigError(f"{name} 的進場名義 {result['max_entry_notional']} 超過 {cap}")
        return _span_row(name, fee_label, oos_bars, result, "oos")

    rows = [
        oos_run("永續1小時 1倍 限價 現貨費率", "做市40/吃單80 bp", store=True),
        oos_run("永續1小時 1倍 市價 現貨費率", "吃單80 bp + 滑點", entry="taker"),
        oos_run(
            "永續1小時 1倍 限價 期貨費率+資金費",
            "做市2/吃單5 bp + 資金費",
            with_funding=True,
            maker_bps=FUTURES_MAKER_BPS,
            taker_bps=FUTURES_TAKER_BPS,
        ),
        oos_run("永續1小時 2倍 限價 現貨費率+資金費", "做市40/吃單80 bp + 資金費", leverage=2.0, with_funding=True),
    ]
    spot = _coverage_spot(client, "BTC/USD", "1h", now)
    spot_bars = spot["bars"]
    spot_signals = trigger_signals(
        [bar.close for bar in spot_bars], chosen["period"], chosen["oversold"], chosen["overbought"]
    )
    spot_result = run_hf(spot_bars, spot_signals, _cfg_from(chosen, lot_decimals=8, ordermin=0.00006, costmin=0.5, store_equity=True))
    if spot_result["max_entry_notional"] > 10.05:
        raise ConfigError("現貨樣本的進場名義超過 10 美元")
    rows.append(_span_row("現貨1小時 1倍 限價 現貨費率", "做市40/吃單80 bp", spot_bars, spot_result, "oos"))

    for symbol, market_name in (("ETH/USD", "PF_ETHUSD"), ("SOL/USD", "PF_SOLUSD")):
        other, _fund, _meta = load_kraken_futures(client, symbol, "1h", now)
        other = sorted(other, key=lambda bar: bar.open_time)
        cut = _split(other, 30)
        held = other[cut:]
        held_signals = trigger_signals([bar.close for bar in other], chosen["period"], chosen["oversold"], chosen["overbought"])[cut:]
        result = run_hf(held, held_signals, _cfg_from(chosen, lot_decimals=3 if symbol.startswith("ETH") else 2))
        rows.append(_span_row(f"{market_name} 最近30日 同一組參數", "做市40/吃單80 bp", held, result, "oos"))

    for timeframe in ("15m", "5m", "1m"):
        short = _coverage_futures(client, "BTC/USD", timeframe, now)
        short_bars = short["bars"]
        short_signals = trigger_signals(
            [bar.close for bar in short_bars], chosen["period"], chosen["oversold"], chosen["overbought"]
        )
        result = run_hf(short_bars, short_signals, _cfg_from(chosen, lot_decimals=4))
        rows.append(_span_row(f"永續{timeframe} 全段 未重挑", "做市40/吃單80 bp", short_bars, result, "short"))

    headline = rows[0]
    if tag == "train_preserved" and headline["total_return"] is not None and headline["total_return"] > 0:
        reason = (
            f"訓練有守住 10 美元。樣本外主列期末 {headline['final_equity']:.4f} 美元，"
            f"報酬 {_pct(headline['total_return'])}。這是 30 日的結果，不是年化。"
        )
    elif headline["total_return"] is not None and headline["total_return"] < 0:
        reason = (
            f"凍結參數在最近 30 日的主列是虧的：期末 {headline['final_equity']:.4f} 美元，"
            f"報酬 {_pct(headline['total_return'])}，最大回撤 {_pct(headline['max_drawdown'])}，"
            f"手續費 {headline['fees_paid']:.4f} 美元。沒有改去用樣本外更好看的那一列。"
            "紙上指令可以跑這組規則，但它不是一條成長建議。"
        )
    else:
        reason = (
            f"訓練標記是 {tag}。樣本外主列期末 {headline['final_equity']:.4f} 美元，"
            f"報酬 {_pct(headline['total_return'])}。數字以這一列為準。"
        )
    train_metrics = next(row for row in train_rows if row["period"] == chosen["period"] and row["oversold"] == chosen["oversold"] and row["overbought"] == chosen["overbought"] and row["sl"] == chosen["sl"] and row["tp"] == chosen["tp"] and row["expire"] == chosen["expire"])
    return {
        "preset_command": "python -m ymzz.cli paper --preset hf",
        "selection_tag": tag,
        "selection_rule_zh": "訓練窗、現貨費率、來回至少 5、權益不低於 0.5、期末至少回到 10，再取回撤最小。否則取仍有 5 筆且期末最高的一組。",
        "selected_on_train": {key: chosen[key] for key in ("period", "oversold", "overbought", "sl", "tp", "expire")},
        "train": {**_window(train_bars), **_public_metrics(train_metrics)},
        "train_grid": [{key: row[key] for key in row if key not in {"equity", "trades"}} for row in train_rows],
        "oos_window": _window(oos_bars),
        "rows": rows,
        "headline_id": headline["id"],
        "coverage": coverage,
        "futures_meta": {
            "symbol": meta.get("futures_symbol"),
            "funding_first": meta.get("funding_first"),
            "funding_last": meta.get("funding_last"),
            "funding_points": meta.get("funding_points"),
            "candle_sha256": meta.get("candle_sha256"),
        },
        "fees": {
            "spot_maker_bps": SPOT_MAKER_BPS,
            "spot_taker_bps": SPOT_TAKER_BPS,
            "slip_bps": SLIP_BPS,
            "futures_maker_bps": FUTURES_MAKER_BPS,
            "futures_taker_bps": FUTURES_TAKER_BPS,
            "futures_schedule_zh": "Incentive Rebate MTF Linear 第一級 makerFee 0.02、takerFee 0.05，按百分數。Consumer Linear 是 0.25/0.25，沒有用。",
        },
        "recommendation": {"id": headline["id"], "reason_zh": reason},
    }


def write_month(root: Path, payload: dict) -> tuple[Path, Path]:
    from ymzz.timeutil import now_utc, to_kst

    moment = now_utc()
    kst = to_kst(moment)
    written = kst.strftime("%Y-%m-%d %H:%M")
    # 曲線只留主列和現貨那一列，短週期的成交不進檔。
    slim_rows = []
    for row in payload["rows"]:
        item = {key: row[key] for key in row if key not in {"equity", "trades"}}
        if row["id"] == payload["headline_id"] or row["name"].startswith("現貨"):
            item["equity"] = row.get("equity") or []
            item["trades"] = row.get("trades") or []
        slim_rows.append(item)
    stored = {**payload, "rows": slim_rows}
    out = root / "results"
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "hf_month.json"
    json_path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    doc = root / "docs" / f"{kst.strftime('%m%d_%H%M')}_seoul_一個月高頻觸發.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(render_month(stored, written), encoding="utf-8")
    return json_path, doc


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    payload = run_month(root)
    json_path, doc_path = write_month(root, payload)
    chosen = payload["selected_on_train"]
    headline = next(row for row in payload["rows"] if row["id"] == payload["headline_id"])
    print(json_path)
    print(doc_path)
    print(payload["selection_tag"], chosen)
    print(
        "oos",
        headline["total_return"],
        headline["max_drawdown"],
        headline["round_trips"],
        headline["win_rate"],
        headline["fees_paid"],
        headline["final_equity"],
    )


if __name__ == "__main__":
    main()
