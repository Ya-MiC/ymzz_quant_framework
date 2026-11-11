"""RSI+MACD 規則。公式與成交假設對齊使用者核准的獨立腳本，資料用合成 K 線。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from ymzz.data import parse_kraken_asset_pairs
from ymzz.engine import Bar, CostConfig, EngineConfig, fill_price, run_backtest
from ymzz.errors import ConfigError, DataSourceError
from ymzz.indicators import ema, macd, rsi
from ymzz.strategies import compute_signals

UTC = timezone.utc
ROOT = Path(__file__).parent / "fixtures"


def _bars(prices, start=None, hours=24):
    t0 = start or datetime(2026, 1, 1, tzinfo=UTC)
    step = timedelta(hours=hours)
    out = []
    for i, (open_, high, low, close) in enumerate(prices):
        opened = t0 + i * step
        out.append(Bar(opened, opened + step, float(open_), float(high), float(low), float(close), closed=True))
    return out


def _cfg(**kw):
    fee = kw.pop("fee_bps", 40.0)
    slip = kw.pop("slippage_bps", 5.0)
    capital = kw.pop("start_equity", kw.pop("capital", 10.0))
    return EngineConfig(
        capital=capital,
        leverage=1,
        instrument="spot",
        cost=CostConfig(fee_bps=fee, slippage_bps=slip),
        sl_pct=kw.pop("sl_pct", 0.05),
        tp_pct=kw.pop("tp_pct", 0.10),
        position_frac=kw.pop("position_frac", 1.0),
        daily_loss_halt_pct=kw.pop("daily_loss_halt_pct", 0.15),
        ordermin=kw.pop("ordermin", 0.0),
        costmin=kw.pop("costmin", 0.0),
        lot_decimals=kw.pop("lot_decimals", 8),
        signal_mode="pulse",
    )


def _run(prices, entry, ind_exit=None, hours=24, **cfg):
    bars = _bars(prices, hours=hours)
    signals = [0.0] * len(bars)
    exits = [0.0] * len(bars)
    for i in entry or []:
        signals[i] = 1.0
    for i in ind_exit or []:
        exits[i] = 1.0
    return run_backtest(bars, signals, [], _cfg(**cfg), exits)


def test_ema_sma_seed_and_rejects_zero():
    out = ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(2.0)
    alpha = 2.0 / 4.0
    assert out[3] == pytest.approx(4.0 * alpha + 2.0 * (1 - alpha))
    with pytest.raises(ConfigError):
        ema([1, 2], 0)


def test_rsi_wilder_short_period_and_extremes():
    closes = [10.0, 11.0, 10.5, 12.0, 11.0]
    out = rsi(closes, 2)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(100.0 - 100.0 / (1.0 + 0.5 / 0.25))
    assert out[3] == pytest.approx(100.0 - 100.0 / (1.0 + 1.0 / 0.125))
    assert rsi([1, 2, 3, 4, 5, 6], 3)[3] == 100.0
    assert rsi([5, 5, 5, 5, 5], 2)[2] == 50.0


def test_macd_is_fast_minus_slow():
    values = np.arange(1.0, 40.0)
    line, sig, hist = macd(values, 3, 6, 3)
    np.testing.assert_allclose(line, ema(values, 3) - ema(values, 6), equal_nan=True)
    np.testing.assert_allclose(hist, line - sig, equal_nan=True)
    assert np.isnan(sig[5])


def _uptrend():
    down = list(np.linspace(120, 80, 40))
    up = list(np.linspace(80.5, 110, 30))
    return np.array(down + up, dtype=float)


def test_combo_entry_needs_golden_cross_and_rsi_band():
    close = _uptrend()
    signals, ind, _ = compute_signals("rsi_macd", close.tolist(), None, False)
    dif, dea, line = ind["dif"], ind["dea"], ind["rsi"]
    exits = ind["_indicator_exit"]
    assert any(flag == 1 for flag in signals) or any(flag == 1 for flag in exits)
    for i, flag in enumerate(signals):
        if not flag:
            continue
        assert dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        assert 40 <= line[i] <= 70
    for i in range(1, len(close)):
        if dif[i] is None or dif[i - 1] is None or dea[i] is None or dea[i - 1] is None or line[i] is None:
            continue
        golden = dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        if golden and not (40 <= line[i] <= 70):
            assert signals[i] == 0
    for i, flag in enumerate(exits):
        if not flag or i == 0:
            continue
        death = dif[i - 1] is not None and dea[i - 1] is not None and dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]
        assert death or line[i] > 75


def test_no_signal_before_indicators_ready():
    signals, _, _ = compute_signals("rsi_macd", [float(i) for i in range(1, 20)], None, False)
    assert sum(signals) == 0


def test_fill_and_same_bar_stop_wins():
    assert fill_price(100.0, 1, 5.0) == pytest.approx(100.05)
    assert fill_price(100.0, -1, 5.0) == pytest.approx(99.95)
    res = _run([(100, 100, 100, 100), (100, 111, 94, 105)], entry=[0], fee_bps=0, slippage_bps=0)
    assert res.round_trips[0]["reason"] == "stop_loss"


def test_signal_fills_next_open_and_not_the_signal_bar():
    res = _run(
        [(100, 100, 100, 100), (101, 101, 101, 101), (102, 102, 102, 102)],
        entry=[0],
        fee_bps=0,
        slippage_bps=0,
        sl_pct=0.5,
        tp_pct=0.5,
    )
    trip = res.round_trips[0]
    assert trip["entry_raw"] == 101
    assert trip["exit_raw"] == 102
    assert trip["reason"] == "end_of_data"
    res_last = _run([(100, 100, 100, 100), (101, 101, 101, 101)], entry=[1], fee_bps=0, slippage_bps=0, sl_pct=0.5, tp_pct=0.5)
    assert res_last.round_trips == []


def test_fees_and_slippage_both_sides():
    res = _run([(100, 100, 100, 100)] * 3, entry=[0], sl_pct=0.5, tp_pct=0.5, position_frac=1)
    trip = res.round_trips[0]
    assert trip["entry_fill"] == pytest.approx(100.05)
    assert trip["exit_fill"] == pytest.approx(99.95)
    assert trip["equity_after"] < 10
    assert trip["fees"] > 0


def test_stop_take_profit_and_indicator_exit():
    stopped = _run([(100, 100, 100, 100), (100, 101, 94, 96), (100, 100, 100, 100)], entry=[0], fee_bps=0, slippage_bps=0)
    assert stopped.round_trips[0]["reason"] == "stop_loss"
    assert stopped.round_trips[0]["exit_raw"] == pytest.approx(95)
    taken = _run([(100, 100, 100, 100), (100, 111, 99, 110)], entry=[0], fee_bps=0, slippage_bps=0)
    assert taken.round_trips[0]["reason"] == "take_profit"
    assert taken.round_trips[0]["exit_raw"] == pytest.approx(110)
    exited = _run(
        [(100, 100, 100, 100), (100, 101, 99, 100.5), (100.5, 101, 100, 100.2)],
        entry=[0],
        ind_exit=[2],
        fee_bps=0,
        slippage_bps=0,
        sl_pct=0.5,
        tp_pct=0.5,
    )
    assert exited.round_trips[0]["reason"] == "indicator_exit"
    assert exited.round_trips[0]["exit_raw"] == pytest.approx(100.2)


def test_below_minimum_is_logged_and_not_counted():
    res = _run(
        [(100_000, 100_000, 100_000, 100_000)] * 3,
        entry=[0],
        position_frac=0.30,
        fee_bps=0,
        slippage_bps=0,
        sl_pct=0.5,
        tp_pct=0.5,
        ordermin=0.00006,
        costmin=0.5,
    )
    assert res.round_trips == []
    assert res.metrics["trade_count"] == 0
    assert res.skips[0]["reason"] == "below minimum"
    assert any("below minimum" in text for text in res.warnings)
    cleared = _run(
        [(70_000, 70_000, 70_000, 70_000)] * 3,
        entry=[0],
        position_frac=1.0,
        fee_bps=0,
        slippage_bps=0,
        sl_pct=0.5,
        tp_pct=0.5,
        ordermin=0.00006,
        costmin=0.5,
    )
    assert len(cleared.round_trips) == 1
    assert cleared.round_trips[0]["qty"] >= 0.00006


def test_daily_circuit_breaker_blocks_same_day_reentry():
    prices = [
        (100, 100, 100, 100),
        (100, 101, 99, 100),
        (80, 81, 79, 80),
        (80, 80, 80, 80),
    ]
    res = _run(prices, entry=[0, 2], hours=1, fee_bps=0, slippage_bps=0, sl_pct=0.05, tp_pct=0.50, position_frac=1)
    assert len(res.round_trips) == 1
    assert res.round_trips[0]["reason"] == "stop_loss"
    assert any(row["reason"] == "daily_circuit_breaker" for row in res.skips)


def test_unclosed_bar_rejected():
    bars = _bars([(100, 100, 100, 100)] * 2)
    bars[-1].closed = False
    with pytest.raises(ConfigError):
        run_backtest(bars, [0, 0], [], _cfg(), [0, 0])


def test_kraken_limits_parser_uses_payload_only():
    payload = json.loads((ROOT / "kraken_assetpairs_xbtusd.json").read_text(encoding="utf-8"))
    limits = parse_kraken_asset_pairs(payload)
    assert limits["ordermin"] == pytest.approx(0.00006)
    assert limits["costmin"] == pytest.approx(0.5)
    assert limits["lot_decimals"] == 8
    with pytest.raises(DataSourceError):
        parse_kraken_asset_pairs({"error": ["EGeneral:Invalid arguments"]})


def test_three_decision_indicators():
    from ymzz.strategies import STRATEGIES

    assert len(STRATEGIES["rsi_macd"].decision_indicators) == 3
