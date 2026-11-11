"""薄回測引擎：下一根開盤成交、手續費、滑點、止盈止損、資金費與清算。

槓桿不是把報酬乘上倍數。現貨槓桿大於 1 會直接拒絕。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    suspended: bool = False
    limit_up: float | None = None
    limit_down: float | None = None


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
    position_frac: float = 1.0
    daily_loss_halt_pct: float | None = None
    ordermin: float = 0.0
    costmin: float = 0.0
    lot_decimals: int | None = None
    signal_mode: str = "target"
    entry_trigger: str = "edge"
    t_plus_one: bool = False
    chase_rise: float | None = None
    chase_bars: int = 20
    max_margin_usd: float | None = 10.0
    max_notional_usd: float | None = 20.0
    max_order_usd: float | None = None
    block_entries_after: datetime | None = None
    require_stop: bool = False

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
        if not (0 < self.position_frac <= 1):
            raise ConfigError("每筆倉位比例必須在 (0, 1]")
        if self.daily_loss_halt_pct is not None and self.daily_loss_halt_pct <= 0:
            raise ConfigError("日虧熔斷比例必須為正；要關掉請不要設定")
        if self.ordermin < 0 or self.costmin < 0:
            raise ConfigError("最小下單量不能是負數")
        if self.lot_decimals is not None and self.lot_decimals < 0:
            raise ConfigError("數量小數位不能是負數")
        if self.signal_mode not in {"target", "pulse"}:
            raise ConfigError("訊號模式只能是 target 或 pulse")
        if self.entry_trigger not in {"edge", "level"}:
            raise ConfigError("進場觸發只能是 edge（等新的交叉）或 level（舊的持倉目標還在就再進）")
        if self.chase_rise is not None and self.chase_rise <= 0:
            raise ConfigError("追高濾網的漲幅必須為正；要關掉請不要設定")
        if self.chase_bars < 2:
            raise ConfigError("追高濾網至少回看 2 根")
        if self.max_order_usd is not None and self.max_order_usd <= 0:
            raise ConfigError("單筆名義上限必須為正")
        if self.max_margin_usd is not None and self.max_margin_usd <= 0:
            raise ConfigError("單筆保證金上限必須為正")
        if self.max_notional_usd is not None and self.max_notional_usd <= 0:
            raise ConfigError("單筆名義上限必須為正")
        if self.require_stop and (self.sl_pct is None or self.sl_pct <= 0):
            raise ConfigError("這條路徑必須附止損")
        return self


def _position_notional_cap(cfg: EngineConfig) -> float | None:
    """整手市場不套用。現貨 1 倍時保證金上限就是名義上限。2 倍時名義可以到保證金的兩倍，但不超過名義上限。"""
    if cfg.lot_size > 0:
        return None
    caps: list[float] = []
    if cfg.max_notional_usd is not None:
        caps.append(cfg.max_notional_usd)
    if cfg.max_margin_usd is not None:
        caps.append(cfg.max_margin_usd * cfg.leverage)
    if cfg.max_order_usd is not None:
        caps.append(cfg.max_order_usd)
    if not caps:
        return None
    return min(caps)


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


def ashare_price_limit_rate(symbol: str, *, st: bool = False) -> float:
    """ST 5%、創業板 300/301 與科創板 688/689 為 20%、其餘主板 10%。只依代碼，不打網路。"""
    code = "".join(ch for ch in symbol if ch.isdigit())
    if st or "ST" in symbol.upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def apply_ashare_limits(bars: list[Bar], rate: float) -> None:
    """用前一交易日收盤標漲停與跌停。停牌日不更新基準收盤。"""
    if rate <= 0 or rate >= 0.5:
        raise ConfigError("漲跌停幅度必須在 0 與 50% 之間")
    prev: float | None = None
    for bar in bars:
        if prev is not None:
            bar.limit_up = round(prev * (1.0 + rate) + 1e-8, 2)
            bar.limit_down = round(prev * (1.0 - rate) + 1e-8, 2)
        if not bar.suspended:
            prev = bar.close


def limit_block(bar: Bar, side: int, raw: float, slip_bps: float) -> str | None:
    """漲停不能買、跌停不能賣。比較的是含滑點的成交價，避免滑點把單子送過板價。"""
    if bar.limit_up is None and bar.limit_down is None:
        return None
    price = fill_price(raw, side, slip_bps)
    if side > 0 and bar.limit_up is not None and price >= bar.limit_up - 1e-8:
        return "limit_up"
    if side < 0 and bar.limit_down is not None and price <= bar.limit_down + 1e-8:
        return "limit_down"
    return None


def floor_qty(qty: float, decimals: int) -> float:
    """數量向下取到小數位，避免四捨五入後超過交易所可下的量。"""
    if decimals < 0:
        raise ConfigError("數量小數位不能是負數")
    scale = 10 ** decimals
    return math.floor(qty * scale + 1e-12) / scale


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
    skips: list[dict] = field(default_factory=list)
    round_trips: list[dict] = field(default_factory=list)

    def to_public(self) -> dict:
        return {
            "trades": self.trades,
            "equity": self.equity,
            "metrics": self.metrics,
            "warnings": self.warnings,
            "skips": self.skips,
            "round_trips": self.round_trips,
        }


def run_backtest(
    bars: list[Bar],
    signals: list[float],
    funding: list[Funding] | None,
    cfg: EngineConfig,
    indicator_exits: list[float] | None = None,
) -> BacktestResult:
    cfg = cfg.validated()
    if len(bars) != len(signals):
        raise ConfigError("訊號長度必須和 K 線相同")
    if indicator_exits is not None and len(indicator_exits) != len(bars):
        raise ConfigError("指標出場長度必須和 K 線相同")
    if any(not bar.closed for bar in bars):
        raise ConfigError("回測拒絕未收盤 K 線。請在資料層先剔除。")
    pulse = cfg.signal_mode == "pulse"
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
    skips: list[dict] = []
    round_trips: list[dict] = []
    equity_rows: list[dict] = []
    fees_paid = 0.0
    funding_paid = 0.0
    liquidations = 0
    blocked_lots = 0
    peak = wallet
    max_dd = 0.0
    fund_idx = 0
    entry_state: dict | None = None
    pending_pulse = 0.0
    reentry_block: float | None = None
    edge_noted = False
    sod_date = None
    sod_equity = wallet
    halted = False
    daily_on = cfg.daily_loss_halt_pct is not None
    warned_minimum = False

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

    def apply_delta(delta: float, raw: float, bar: Bar, reason: str, force: bool = False, when: datetime | None = None) -> bool:
        nonlocal wallet, qty, entry, fees_paid, liquidations, entry_state
        if abs(delta) < 1e-16:
            return False
        prev_qty = qty
        prev_entry = entry
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
        stamp_time = when or bar.open_time
        push_trade(action, delta, price, fee + stamp, reason, slip_bps, stamp_time)
        if prev_qty == 0 and qty != 0:
            entry_state = {
                "time": stamp_time,
                "raw": raw,
                "fill": price,
                "fee": fee + stamp,
            }
        elif prev_qty != 0 and qty == 0 and entry_state is not None:
            exit_fee = fee + stamp
            side_sign = 1 if prev_qty > 0 else -1
            pnl = side_sign * (price - prev_entry) * abs(prev_qty) - entry_state["fee"] - exit_fee
            round_trips.append(
                {
                    "entry_time": entry_state["time"].isoformat(),
                    "exit_time": stamp_time.isoformat(),
                    "qty": abs(prev_qty),
                    "entry_raw": entry_state["raw"],
                    "entry_fill": entry_state["fill"],
                    "exit_raw": raw,
                    "exit_fill": price,
                    "reason": reason,
                    "pnl": pnl,
                    "fees": entry_state["fee"] + exit_fee,
                    "entry_fee": entry_state["fee"],
                    "exit_fee": exit_fee,
                    "equity_after": wallet,
                }
            )
            entry_state = None
        elif prev_qty != 0 and qty != 0 and abs(qty) > abs(prev_qty) and entry_state is not None:
            entry_state["fee"] += fee + stamp
        return True

    def target_qty(raw: float, exposure: float, bar: Bar) -> float:
        nonlocal order_capped
        if abs(exposure) < 1e-12:
            return 0.0
        mark_equity = equity_at(raw)
        if mark_equity <= 0:
            return 0.0
        lev = cfg.leverage
        fraction = abs(exposure) * cfg.position_frac
        fee_rate = cfg.cost.fee_bps / 10000.0
        notional = mark_equity * lev * fraction / (1.0 + fee_rate * lev * fraction)
        cap = _position_notional_cap(cfg)
        if cap is not None and notional > cap:
            notional = cap
            if not order_capped:
                order_capped = True
                warnings.append("單筆名義超過上限，已壓回上限。帳戶變大也不會把這一張單放大。")
        slip_bps = total_slip_bps(notional, bar.quote_volume, cfg.cost)
        side = 1 if exposure > 0 else -1
        price = fill_price(raw, side, slip_bps)
        desired = side * (notional / price)
        if cfg.lot_size > 0:
            units = int(abs(desired) // cfg.lot_size)
            if units < 1:
                return 0.0
            desired = side * units * cfg.lot_size
        if cfg.lot_decimals is not None:
            desired = side * floor_qty(abs(desired), cfg.lot_decimals)
        return desired

    def log_skip(bar: Bar, reason: str, **extra) -> None:
        nonlocal warned_minimum
        row = {"time": bar.open_time.isoformat(), "reason": reason, **extra}
        skips.append(row)
        trades.append(
            {
                "time": bar.open_time.isoformat(),
                "action": "skip",
                "qty": float(extra.get("qty") or 0.0),
                "price": float(extra.get("price") or bar.open),
                "fee": 0.0,
                "slippage_bps": 0.0,
                "reason": reason,
                "wallet_after": wallet,
                "position_after": qty,
                "equity_after": equity_at(bar.open),
            }
        )
        if reason == "below minimum" and not warned_minimum:
            warned_minimum = True
            warnings.append("有進場低於交易所最小下單量，已記為 below minimum，沒有算進成交筆數。")

    def under_minimum(delta: float, raw: float, bar: Bar) -> bool:
        if cfg.ordermin <= 0 and cfg.costmin <= 0:
            return False
        if qty != 0 and delta * qty < 0 and abs(delta) <= abs(qty) + 1e-12:
            return False
        side = 1 if delta > 0 else -1
        slip_bps = total_slip_bps(abs(delta) * raw, bar.quote_volume, cfg.cost)
        price = fill_price(raw, side, slip_bps)
        notional = abs(delta) * price
        return abs(delta) <= 1e-16 or abs(delta) + 1e-15 < cfg.ordermin or notional + 1e-9 < cfg.costmin

    def try_signal_delta(delta: float, raw: float, bar: Bar) -> bool:
        """進場才受日虧熔斷與最小下單量限制。減倉與平倉照常。"""
        increasing = qty == 0 or (delta * qty > 0) or (qty != 0 and abs(qty + delta) > abs(qty) + 1e-12)
        if increasing and cfg.block_entries_after is not None and bar.open_time >= cfg.block_entries_after:
            log_skip(bar, "global_drawdown_breaker", price=raw)
            return False
        if increasing and daily_on and halted:
            log_skip(bar, "daily_circuit_breaker", price=raw, budget=equity_at(raw) * cfg.position_frac)
            return False
        side = 1 if delta > 0 else -1
        blocked = limit_block(bar, side, raw, total_slip_bps(abs(delta) * raw, bar.quote_volume, cfg.cost))
        if blocked:
            log_skip(bar, blocked, price=raw, qty=abs(delta))
            return False
        if increasing and under_minimum(delta, raw, bar):
            side = 1 if delta > 0 else -1
            slip_bps = total_slip_bps(abs(delta) * raw, bar.quote_volume, cfg.cost)
            price = fill_price(raw, side, slip_bps)
            log_skip(
                bar,
                "below minimum",
                qty=abs(delta),
                price=price,
                ordermin=cfg.ordermin,
                costmin=cfg.costmin,
                notional=abs(delta) * price,
                budget=equity_at(raw) * cfg.position_frac,
            )
            return False
        return apply_delta(delta, raw, bar, "signal")

    def open_to_exposure(exposure: float, bar: Bar) -> bool:
        nonlocal bought_this_bar, executed
        if cfg.chase_rise is not None and qty == 0 and abs(exposure) > 0 and cursor_i >= cfg.chase_bars:
            window = bars[cursor_i - cfg.chase_bars : cursor_i]
            if exposure > 0:
                floor = min(item.low for item in window)
                if floor > 0 and bar.open > floor * (1.0 + cfg.chase_rise):
                    log_skip(bar, "chase_filter", price=bar.open, floor=floor, rise=bar.open / floor - 1.0)
                    return False
            else:
                ceiling = max(item.high for item in window)
                if ceiling > 0 and bar.open < ceiling * (1.0 - cfg.chase_rise):
                    log_skip(bar, "chase_filter", price=bar.open, ceiling=ceiling, drop=1.0 - bar.open / ceiling)
                    return False
        desired = target_qty(bar.open, exposure, bar)
        if abs(exposure) > 1e-9 and abs(desired) < 1e-16 and cfg.lot_size > 0 and cfg.ordermin <= 0 and cfg.costmin <= 0:
            blocked_lots_add()
            return False
        delta = desired - qty
        if abs(delta) <= 1e-12:
            if abs(exposure) > 1e-9 and qty == 0 and (cfg.ordermin > 0 or cfg.costmin > 0):
                log_skip(bar, "below minimum", qty=0.0, price=bar.open, ordermin=cfg.ordermin, costmin=cfg.costmin, notional=0.0)
            return False
        ok = try_signal_delta(delta, bar.open, bar)
        if ok:
            bought_this_bar = delta > 0 and qty != 0
            executed = exposure if qty != 0 else 0.0
        elif qty == 0:
            executed = 0.0
        return ok

    def blocked_lots_add() -> None:
        nonlocal blocked_lots
        blocked_lots += 1
        if blocked_lots == 1:
            warnings.append("資金不足一手，這次沒有拆成零股。現貨也不會用槓桿補一手。")

    def remember_exit(closed_exposure: float) -> None:
        nonlocal executed, reentry_block
        executed = 0.0
        if cfg.entry_trigger == "edge" and abs(closed_exposure) > 1e-9:
            reentry_block = closed_exposure

    cursor_i = 0
    order_capped = False
    for i, bar in enumerate(bars):
        cursor_i = i
        bought_this_bar = False
        if daily_on:
            day = bar.open_time.astimezone(timezone.utc).date()
            if day != sod_date:
                sod_date = day
                sod_equity = cfg.capital if i == 0 else equity_rows[-1]["equity"]
                halted = False
        if bar.suspended:
            pending = None
            pending_pulse = 0.0
            log_skip(bar, "suspended", price=bar.open)
        else:
            if pulse:
                if pending_pulse != 0 and qty == 0:
                    open_to_exposure(pending_pulse, bar)
                    reentry_block = None
                pending_pulse = 0.0
            elif pending is not None:
                desired_exp = float(pending)
                pending = None
                same_target = reentry_block is not None and abs(desired_exp - reentry_block) <= 1e-9
                if cfg.entry_trigger == "edge" and same_target and qty == 0:
                    if not edge_noted:
                        edge_noted = True
                        warnings.append("出場後目標曝險沒變，預設要等訊號改向才再進場。entry_trigger=level 才會沿用舊的下一根再進。")
                else:
                    changed = abs(desired_exp - executed) > 1e-9 or (
                        cfg.entry_trigger == "level" and abs(desired_exp) > 1e-9 and qty == 0
                    )
                    if changed:
                        open_to_exposure(desired_exp, bar)
                    if reentry_block is not None and not same_target:
                        reentry_block = None

            locked_today = bought_this_bar and (cfg.t_plus_one or not cfg.intrabar_exit_on_entry)
            allow_exit = qty != 0 and not locked_today
            if allow_exit:
                side = 1 if qty > 0 else -1
                sl, tp = _levels(side, entry, cfg.tp_pct, cfg.sl_pct)
                liq = None
                if cfg.instrument == "perpetual":
                    liq = liquidation_price(entry, abs(qty), wallet, cfg.mmr, side)
                decision = decide_exit(side, bar.open, bar.high, bar.low, sl, tp, liq)
                if decision is not None:
                    reason, raw = decision
                    close_side = -1 if qty > 0 else 1
                    blocked = limit_block(bar, close_side, raw, total_slip_bps(abs(qty) * raw, bar.quote_volume, cfg.cost))
                    if blocked:
                        log_skip(bar, blocked, price=raw, qty=abs(qty))
                    else:
                        # 開盤跳空用開盤時間；打在高低點的止盈止損算在這根收盤。
                        filled_at = bar.open_time if raw == bar.open else bar.close_time
                        closed_exposure = executed
                        apply_delta(-qty, raw, bar, reason, force=True, when=filled_at)
                        remember_exit(closed_exposure)

            if indicator_exits is not None and qty != 0 and not locked_today and float(indicator_exits[i] or 0) > 0:
                blocked = limit_block(bar, -1 if qty > 0 else 1, bar.close, total_slip_bps(abs(qty) * bar.close, bar.quote_volume, cfg.cost))
                if blocked:
                    log_skip(bar, blocked, price=bar.close, qty=abs(qty))
                else:
                    closed_exposure = executed
                    apply_delta(-qty, bar.close, bar, "indicator_exit", force=True, when=bar.close_time)
                    remember_exit(closed_exposure)

        if qty != 0 and cfg.instrument == "perpetual" and not bar.suspended:
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
                closed_exposure = executed
                wallet = 0.0
                qty = 0.0
                entry = 0.0
                liquidations += 1
                remember_exit(closed_exposure)
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
        if daily_on and sod_equity > 0 and (sod_equity - eq) / sod_equity >= cfg.daily_loss_halt_pct - 1e-15:
            halted = True
            equity_rows[-1]["halted"] = True
        if pulse:
            if qty == 0 and i < len(bars) - 1 and abs(float(signals[i])) > 1e-12:
                pending_pulse = 1.0 if float(signals[i]) > 0 else -1.0
        elif i < len(bars) - 1:
            pending = float(signals[i])

    if pulse and qty != 0:
        last = bars[-1]
        apply_delta(-qty, last.close, last, "end_of_data", force=True, when=last.close_time)
        eq = equity_at(last.close)
        if eq > peak:
            peak = eq
        drawdown = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)
        equity_rows[-1]["equity"] = eq
        equity_rows[-1]["wallet"] = wallet
        equity_rows[-1]["position"] = qty
        equity_rows[-1]["drawdown"] = drawdown
        warnings.append("樣本結束時仍有持倉，已按最後一根收盤價平倉（含滑點與手續費）。")

    final_equity = equity_rows[-1]["equity"] if equity_rows else wallet
    price_note = "未平倉以最後一根收盤做市值，帳本上仍是持倉，沒有假造成交。"
    if qty != 0:
        warnings.append(price_note)
    wins = sum(1 for trip in round_trips if trip["pnl"] > 0)
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
        "position_frac": cfg.position_frac,
        "round_trips": len(round_trips),
        "wins": wins,
        "win_rate": (wins / len(round_trips)) if round_trips else None,
        "skips_below_minimum": sum(1 for row in skips if row["reason"] == "below minimum"),
        "skips_halt": sum(1 for row in skips if row["reason"] == "daily_circuit_breaker"),
        "skips_chase": sum(1 for row in skips if row["reason"] == "chase_filter"),
        "skips_global": sum(1 for row in skips if row["reason"] == "global_drawdown_breaker"),
        "max_order_usd": cfg.max_order_usd,
        "max_margin_usd": cfg.max_margin_usd,
        "max_notional_usd": cfg.max_notional_usd,
    }
    return BacktestResult(trades, equity_rows, metrics, warnings, skips, round_trips)


def sample_buy_hold(
    bars: list[Bar],
    capital: float,
    fee_bps: float,
    slippage_bps: float,
    lot_decimals: int | None = 8,
    ordermin: float = 0.0,
    costmin: float = 0.0,
) -> dict:
    """從第一根開盤買到最後收盤，用滿倉，同一套手續費與滑點。這是樣本期持有，不是訊號後才買。"""
    if not bars or capital <= 0:
        return {"possible": False, "reason": "no_bars", "return_pct": None, "end_equity": None}
    first, last = bars[0], bars[-1]
    fee = fee_bps / 10000.0
    px_in = fill_price(first.open, 1, slippage_bps)
    raw_qty = capital / (px_in * (1.0 + fee)) if px_in > 0 else 0.0
    qty = floor_qty(raw_qty, lot_decimals) if lot_decimals is not None else raw_qty
    notional = qty * px_in
    if qty <= 0 or (ordermin > 0 and qty < ordermin) or (costmin > 0 and notional < costmin):
        return {
            "possible": False,
            "reason": "below minimum",
            "qty": qty,
            "entry_fill": px_in,
            "return_pct": None,
            "end_equity": None,
            "raw_move_pct": (last.close / first.open - 1.0) * 100.0 if first.open else None,
        }
    in_fee = notional * fee
    cash = capital - notional - in_fee
    px_out = fill_price(last.close, -1, slippage_bps)
    out_fee = qty * px_out * fee
    end = cash + qty * px_out - out_fee
    return {
        "possible": True,
        "qty": qty,
        "entry_time": first.open_time.isoformat(),
        "exit_time": last.close_time.isoformat(),
        "entry_raw": first.open,
        "entry_fill": px_in,
        "exit_raw": last.close,
        "exit_fill": px_out,
        "fees": in_fee + out_fee,
        "end_equity": end,
        "return_pct": (end / capital - 1.0) * 100.0,
        "raw_move_pct": (last.close / first.open - 1.0) * 100.0,
    }
