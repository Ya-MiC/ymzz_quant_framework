"""薄回測引擎：下一根開盤成交、手續費、滑點、止盈止損、資金費與清算。

槓桿不是把報酬乘上倍數。現貨槓桿大於 1 會直接拒絕。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ymzz.errors import ConfigError, LeverageError

ALLOWED_LEVERAGE = (1.0, 1.5, 2.0)
MAX_LEVERAGE = 2.0


def enforce_leverage(leverage: float, instrument: str) -> float:
    """伺服器與引擎共用。只接受 1、1.5、2，永續才可以大於 1。"""
    if isinstance(leverage, bool) or not isinstance(leverage, (int, float)):
        raise LeverageError("槓桿必須是 1、1.5 或 2")
    lev = float(leverage)
    if not any(abs(lev - item) < 1e-9 for item in ALLOWED_LEVERAGE):
        raise LeverageError(f"槓桿只能是 1、1.5 或 2，收到 {leverage}。上限 2 倍，後端拒絕更高或任意中間值。")
    if lev - MAX_LEVERAGE > 1e-9:
        raise LeverageError("槓桿上限是 2 倍")
    if instrument != "perpetual" and lev > 1.0 + 1e-9:
        raise LeverageError("現貨沒有原生保證金。槓桿大於 1 只允許標明的永續模式，而且不會把現貨報酬直接乘上倍數。")
    if instrument not in {"spot", "perpetual"}:
        raise LeverageError("工具類型只能是現貨或永續")
    return lev


@dataclass
class CostConfig:
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    impact_coef: float = 0.0
    impact_cap_bps: float = 200.0
    stamp_tax_bps: float = 0.0
    min_commission: float = 0.0

    def validate(self) -> None:
        for name, value in (
            ("手續費", self.fee_bps),
            ("滑點", self.slippage_bps),
            ("量能衝擊係數", self.impact_coef),
            ("印花稅", self.stamp_tax_bps),
            ("最低佣金", self.min_commission),
        ):
            if value < 0:
                raise ConfigError(f"{name} 不能是負數")
        if self.impact_cap_bps <= 0:
            raise ConfigError("滑點上限必須為正")


@dataclass
class Bar:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    quote_volume: float | None = None
    closed: bool = True


@dataclass
class Funding:
    time: datetime
    rate: float


@dataclass
class EngineConfig:
    capital: float
    leverage: float
    instrument: str
    cost: CostConfig = field(default_factory=CostConfig)
    tp_pct: float | None = 0.10
    sl_pct: float | None = 0.05
    mmr: float = 0.004
    lot_size: float = 0.0
    intrabar_exit_on_entry: bool = True
    allow_short: bool = False

    def validated(self) -> "EngineConfig":
        self.leverage = enforce_leverage(self.leverage, self.instrument)
        self.cost.validate()
        if self.capital <= 0:
            raise ConfigError("本金必須大於 0")
        if self.mmr < 0 or self.mmr >= 0.2:
            raise ConfigError("維持保證金率須在 0 到 0.2 之間")
        if self.tp_pct is not None and self.tp_pct <= 0:
            self.tp_pct = None
        if self.sl_pct is not None and self.sl_pct <= 0:
            self.sl_pct = None
        if self.lot_size < 0:
            raise ConfigError("手數不能是負數")
        if self.instrument != "perpetual":
            self.allow_short = False
        return self


def total_slip_bps(notional: float, quote_volume: float | None, cost: CostConfig) -> float:
    impact = 0.0
    if cost.impact_coef > 0:
        if quote_volume is None or quote_volume <= 0:
            raise ConfigError("已設定量能滑點，但這根 K 線沒有成交額。拒絕用估計成交額填補。")
        impact = min(cost.impact_cap_bps, cost.impact_coef * (abs(notional) / quote_volume) * 10000.0)
    return cost.slippage_bps + impact


def fill_price(raw: float, side: int, slip_bps: float) -> float:
    """side > 0 是買入，成交價更差。"""
    rate = slip_bps / 10000.0
    if side > 0:
        return raw * (1.0 + rate)
    return raw * (1.0 - rate)


def liquidation_price(entry: float, qty_abs: float, wallet: float, mmr: float, side: int) -> float | None:
    """由目前錢包（不含未實現）反推清算價。保證金不足時不會追加。"""
    if qty_abs <= 0 or entry <= 0:
        return None
    if side > 0:
        denom = qty_abs * (1.0 - mmr)
        if denom <= 0:
            return None
        price = (qty_abs * entry - wallet) / denom
    else:
        price = (wallet + qty_abs * entry) / (qty_abs * (1.0 + mmr))
    if price <= 0:
        return None
    return price


def decide_exit(
    side: int,
    open_: float,
    high: float,
    low: float,
    sl: float | None,
    tp: float | None,
    liq: float | None,
) -> tuple[str, float] | None:
    """決定本根是否出場，以及尚未加滑點的價格。

    開盤已經跳空穿過某一側時，用開盤價（跳空先發生）。
    開盤在止盈和止損之間、而高低點兩邊都碰到時，先止損，不吃止盈。
    多單同時碰到止損與清算時，取價格較高的那一道（下跌時先碰到）。
    """

    def _long() -> tuple[str, float] | None:
        if sl is not None and open_ <= sl and (liq is None or sl >= liq):
            return "stop_loss", open_
        if liq is not None and open_ <= liq and (sl is None or liq > sl):
            return "liquidation", open_
        if sl is not None and liq is not None and open_ <= min(sl, liq):
            level_reason = "stop_loss" if sl >= liq else "liquidation"
            return level_reason, open_
        if tp is not None and open_ >= tp:
            return "take_profit", open_
        sl_hit = sl is not None and low <= sl
        liq_hit = liq is not None and low <= liq
        tp_hit = tp is not None and high >= tp
        if tp_hit and (sl_hit or liq_hit):
            tp_hit = False
        candidates: list[tuple[str, float]] = []
        if sl_hit and sl is not None:
            candidates.append(("stop_loss", sl))
        if liq_hit and liq is not None:
            candidates.append(("liquidation", liq))
        if candidates:
            reason, level = max(candidates, key=lambda item: item[1])
            return reason, open_ if open_ <= level else level
        if tp_hit and tp is not None:
            return "take_profit", open_ if open_ >= tp else tp
        return None

    def _short() -> tuple[str, float] | None:
        if sl is not None and open_ >= sl and (liq is None or sl <= liq):
            return "stop_loss", open_
        if liq is not None and open_ >= liq and (sl is None or liq < sl):
            return "liquidation", open_
        if sl is not None and liq is not None and open_ >= max(sl, liq):
            level_reason = "stop_loss" if sl <= liq else "liquidation"
            return level_reason, open_
        if tp is not None and open_ <= tp:
            return "take_profit", open_
        sl_hit = sl is not None and high >= sl
        liq_hit = liq is not None and high >= liq
        tp_hit = tp is not None and low <= tp
        if tp_hit and (sl_hit or liq_hit):
            tp_hit = False
        candidates = []
        if sl_hit and sl is not None:
            candidates.append(("stop_loss", sl))
        if liq_hit and liq is not None:
            candidates.append(("liquidation", liq))
        if candidates:
            reason, level = min(candidates, key=lambda item: item[1])
            return reason, open_ if open_ >= level else level
        if tp_hit and tp is not None:
            return "take_profit", open_ if open_ <= tp else tp
        return None

    return _long() if side > 0 else _short()


def _levels(side: int, entry: float, tp_pct: float | None, sl_pct: float | None):
    if entry <= 0 or side == 0:
        return None, None
    if side > 0:
        sl = entry * (1.0 - sl_pct) if sl_pct else None
        tp = entry * (1.0 + tp_pct) if tp_pct else None
    else:
        sl = entry * (1.0 + sl_pct) if sl_pct else None
        tp = entry * (1.0 - tp_pct) if tp_pct else None
    return sl, tp


def _commission(notional: float, cost: CostConfig) -> float:
    proportional = abs(notional) * cost.fee_bps / 10000.0
    if cost.min_commission > 0:
        return max(proportional, cost.min_commission)
    return proportional


@dataclass
class BacktestResult:
    trades: list[dict]
    equity: list[dict]
    metrics: dict
    warnings: list[str]

    def to_public(self) -> dict:
        return {
            "trades": self.trades,
            "equity": self.equity,
            "metrics": self.metrics,
            "warnings": self.warnings,
        }


def run_backtest(
    bars: list[Bar],
    signals: list[float],
    funding: list[Funding] | None,
    cfg: EngineConfig,
) -> BacktestResult:
    cfg = cfg.validated()
    if len(bars) != len(signals):
        raise ConfigError("訊號長度必須和 K 線相同")
    if any(not bar.closed for bar in bars):
        raise ConfigError("回測拒絕未收盤 K 線。請在資料層先剔除。")
    funding = sorted(funding or [], key=lambda item: item.time)
    warnings: list[str] = []
    if cfg.instrument != "perpetual" and any(sig < -1e-12 for sig in signals):
        warnings.append("現貨不做空。負的目標曝險已改成平倉，沒有用借券或合約去模擬。")
        signals = [0.0 if sig < 0 else sig for sig in signals]
    if not cfg.allow_short:
        signals = [0.0 if sig < 0 else sig for sig in signals]

    wallet = float(cfg.capital)
    qty = 0.0
    entry = 0.0
    executed = 0.0
    pending: float | None = None
    bought_this_bar = False
    trades: list[dict] = []
    equity_rows: list[dict] = []
    fees_paid = 0.0
    funding_paid = 0.0
    liquidations = 0
    blocked_lots = 0
    peak = wallet
    max_dd = 0.0
    fund_idx = 0

    def equity_at(mark: float) -> float:
        if qty == 0:
            return wallet
        return wallet + qty * (mark - entry)

    def push_trade(action: str, trade_qty: float, price: float, fee: float, reason: str, slip_bps: float, when: datetime):
        trades.append(
            {
                "time": when.isoformat(),
                "action": action,
                "qty": trade_qty,
                "price": price,
                "fee": fee,
                "slippage_bps": slip_bps,
                "reason": reason,
                "wallet_after": wallet,
                "position_after": qty,
                "equity_after": equity_at(price),
            }
        )

    def apply_delta(delta: float, raw: float, bar: Bar, reason: str, force: bool = False) -> bool:
        nonlocal wallet, qty, entry, fees_paid, liquidations
        if abs(delta) < 1e-16:
            return False
        side = 1 if delta > 0 else -1
        notional_est = abs(delta) * raw
        slip_bps = total_slip_bps(notional_est, bar.quote_volume, cfg.cost)
        price = fill_price(raw, side, slip_bps)
        fee = _commission(abs(delta) * price, cfg.cost)
        realized = 0.0
        stamp = 0.0
        if qty != 0 and delta * qty < 0:
            closing = min(abs(qty), abs(delta))
            realized = (closing if qty > 0 else -closing) * (price - entry)
        if cfg.cost.stamp_tax_bps > 0 and side < 0 and qty > 0:
            sold = min(abs(delta), qty)
            stamp = sold * price * cfg.cost.stamp_tax_bps / 10000.0
        next_wallet = wallet + realized - fee - stamp
        if next_wallet < -1e-9 and not force and not (reason in {"liquidation", "stop_loss"} and qty * delta < 0):
            if delta * qty >= 0 or abs(delta) > abs(qty):
                warnings.append("現金不夠支付這筆加倉的手續費，已跳過，沒有追加資金。")
                return False
        if next_wallet < 0:
            next_wallet = 0.0
            if reason == "liquidation":
                warnings.append("成交價穿過破產價，權益歸零。沒有追加保證金，也沒有把負債留在帳上。")
        new_qty = qty + delta
        if abs(new_qty) < 1e-12:
            new_qty = 0.0
            new_entry = 0.0
        elif qty == 0 or qty * delta > 0:
            new_entry = price if qty == 0 else (abs(qty) * entry + abs(delta) * price) / abs(new_qty)
        elif qty * new_qty < 0:
            new_entry = price
        else:
            new_entry = entry
        wallet = next_wallet
        fees_paid += fee + stamp
        qty = new_qty
        entry = new_entry
        if reason == "liquidation":
            liquidations += 1
            qty = 0.0
            entry = 0.0
        action = "buy" if side > 0 else "sell"
        push_trade(action, delta, price, fee + stamp, reason, slip_bps, bar.open_time)
        return True

    def target_qty(raw: float, exposure: float, bar: Bar) -> float:
        if abs(exposure) < 1e-12:
            return 0.0
        mark_equity = equity_at(raw)
        if mark_equity <= 0:
            return 0.0
        lev = cfg.leverage
        fraction = abs(exposure)
        fee_rate = cfg.cost.fee_bps / 10000.0
        notional = mark_equity * lev * fraction / (1.0 + fee_rate * lev * fraction)
        slip_bps = total_slip_bps(notional, bar.quote_volume, cfg.cost)
        side = 1 if exposure > 0 else -1
        price = fill_price(raw, side, slip_bps)
        desired = side * (notional / price)
        if cfg.lot_size > 0:
            units = int(abs(desired) // cfg.lot_size)
            if units < 1:
                return 0.0
            desired = side * units * cfg.lot_size
        return desired

    for i, bar in enumerate(bars):
        bought_this_bar = False
        if pending is not None:
            desired_exp = float(pending)
            pending = None
            if abs(desired_exp - executed) > 1e-9 or (abs(desired_exp) > 1e-9 and qty == 0):
                desired = target_qty(bar.open, desired_exp, bar)
                if abs(desired_exp) > 1e-9 and abs(desired) < 1e-16 and cfg.lot_size > 0:
                    blocked_lots += 1
                    if blocked_lots == 1:
                        warnings.append("資金不足一手，這次沒有拆成零股。現貨也不會用槓桿補一手。")
                else:
                    delta = desired - qty
                    if abs(delta) > 1e-12:
                        if apply_delta(delta, bar.open, bar, "signal"):
                            bought_this_bar = delta > 0
                            executed = desired_exp if qty != 0 else 0.0
                        else:
                            executed = 0.0 if qty == 0 else executed

        allow_exit = qty != 0 and (cfg.intrabar_exit_on_entry or not bought_this_bar)
        if allow_exit:
            side = 1 if qty > 0 else -1
            sl, tp = _levels(side, entry, cfg.tp_pct, cfg.sl_pct)
            liq = None
            if cfg.instrument == "perpetual":
                liq = liquidation_price(entry, abs(qty), wallet, cfg.mmr, side)
            decision = decide_exit(side, bar.open, bar.high, bar.low, sl, tp, liq)
            if decision is not None:
                reason, raw = decision
                apply_delta(-qty, raw, bar, reason, force=True)
                executed = 0.0

        if qty != 0 and cfg.instrument == "perpetual":
            while fund_idx < len(funding) and funding[fund_idx].time < bar.open_time:
                fund_idx += 1
            cursor = fund_idx
            while cursor < len(funding) and funding[cursor].time < bar.close_time:
                event = funding[cursor]
                if bar.open_time <= event.time < bar.close_time:
                    payment = qty * bar.close * event.rate
                    wallet -= payment
                    funding_paid += payment
                    trades.append(
                        {
                            "time": event.time.isoformat(),
                            "action": "funding",
                            "qty": qty,
                            "price": bar.close,
                            "fee": payment,
                            "slippage_bps": 0.0,
                            "reason": "funding_mark_close",
                            "wallet_after": wallet,
                            "position_after": qty,
                            "equity_after": equity_at(bar.close),
                        }
                    )
                cursor += 1
            fund_idx = cursor
            if wallet < 0 and qty != 0:
                wallet = 0.0
                qty = 0.0
                entry = 0.0
                executed = 0.0
                liquidations += 1
                warnings.append("資金費使錢包低於零，部位被視為清算。沒有追加保證金。")

        eq = equity_at(bar.close)
        if eq > peak:
            peak = eq
        drawdown = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)
        equity_rows.append(
            {
                "time": bar.close_time.isoformat(),
                "equity": eq,
                "wallet": wallet,
                "drawdown": drawdown,
                "position": qty,
            }
        )
        if i < len(bars) - 1:
            pending = float(signals[i])

    final_equity = equity_rows[-1]["equity"] if equity_rows else wallet
    price_note = "未平倉以最後一根收盤做市值，帳本上仍是持倉，沒有假造成交。"
    if qty != 0:
        warnings.append(price_note)
    metrics = {
        "initial_capital": cfg.capital,
        "final_equity": final_equity,
        "total_return": (final_equity / cfg.capital - 1.0) if cfg.capital else None,
        "max_drawdown": max_dd,
        "fees_paid": fees_paid,
        "funding_paid": funding_paid,
        "liquidations": liquidations,
        "trade_count": sum(1 for row in trades if row["action"] in {"buy", "sell"}),
        "entries_blocked_by_lot": blocked_lots,
        "open_qty": qty,
        "open_entry": entry if qty else None,
        "leverage": cfg.leverage,
        "instrument": cfg.instrument,
        "unrealized_pnl": (final_equity - wallet) if equity_rows else 0.0,
        "mark_to_market_not_a_fill": qty != 0,
    }
    return BacktestResult(trades, equity_rows, metrics, warnings)
