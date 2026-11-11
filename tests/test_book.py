"""單筆上限、空單、資金費與多槽規則。不打網路。"""

from datetime import datetime, timedelta, timezone

import pytest

from ymzz.book import cagr, choose_row
from ymzz.data import parse_kraken_funding_rates, parse_kraken_futures_candles
from ymzz.engine import Bar, CostConfig, EngineConfig, Funding, run_backtest
from ymzz.errors import ConfigError
from ymzz.paper import LiveRefused, kraken_pair, order_commands
from ymzz.strategies import compute_signals

UTC = timezone.utc


def _day(i, price, high=None, low=None):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
    return Bar(start, start + timedelta(days=1), price, high if high is not None else price, low if low is not None else price, price, 1.0, 1_000_000.0, True)


def test_spot_notional_stays_at_10_and_perp_2x_stays_at_20():
    bars = [_day(i, 100) for i in range(4)]
    spot = run_backtest(bars, [1, 1, 1, 1], [], EngineConfig(capital=1000, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None))
    buy = next(row for row in spot.trades if row["action"] == "buy")
    assert abs(buy["qty"]) * buy["price"] == pytest.approx(10, abs=0.05)
    perp = run_backtest(
        bars,
        [1, 1, 1, 1],
        [],
        EngineConfig(capital=1000, leverage=2, instrument="perpetual", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None),
    )
    opened = next(row for row in perp.trades if row["action"] == "buy")
    assert abs(opened["qty"]) * opened["price"] == pytest.approx(20, abs=0.05)


def test_short_perp_uses_a_stop_and_spot_does_not_borrow():
    bars = [_day(i, 100) for i in range(4)]
    with pytest.raises(ConfigError):
        run_backtest(bars, [-1, -1, -1, -1], [], EngineConfig(capital=10, leverage=1, instrument="perpetual", cost=CostConfig(0, 0), sl_pct=None, require_stop=True))
    short = run_backtest(
        bars,
        [-1, -1, -1, -1],
        [],
        EngineConfig(capital=10, leverage=1, instrument="perpetual", cost=CostConfig(0, 0), tp_pct=None, sl_pct=0.05, allow_short=True),
    )
    assert any(row["action"] == "sell" and row["position_after"] < 0 for row in short.trades)
    spot = run_backtest(bars, [-1, -1, -1, -1], [], EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=None, sl_pct=0.05))
    assert not any(row["position_after"] < 0 for row in spot.trades)


def test_chase_filter_blocks_a_short_after_a_dump():
    bars = [_day(i, 100) for i in range(21)]
    bars.append(_day(21, 60))
    blocked = run_backtest(
        bars,
        [0.0] * 20 + [-1.0, 0.0],
        [],
        EngineConfig(capital=10, leverage=1, instrument="perpetual", cost=CostConfig(0, 0), tp_pct=None, sl_pct=0.05, allow_short=True, chase_rise=0.30, chase_bars=20),
    )
    assert any(row["reason"] == "chase_filter" for row in blocked.skips)
    assert not any(row["position_after"] < 0 for row in blocked.trades)


def test_funding_payment_uses_the_relative_rate():
    bars = [_day(0, 100), _day(1, 100)]
    event = Funding(datetime(2026, 1, 2, 12, tzinfo=UTC), 0.001)
    held = run_backtest(
        bars,
        [1, 1],
        [event],
        EngineConfig(capital=10, leverage=1, instrument="perpetual", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None, max_notional_usd=10, max_margin_usd=10),
    )
    funding = next(row for row in held.trades if row["action"] == "funding")
    qty = held.metrics["open_qty"]
    assert funding["fee"] == pytest.approx(qty * 100 * 0.001)


def test_live_buy_over_the_cap_is_refused_and_usdc_pairs_map():
    with pytest.raises(LiveRefused):
        order_commands(live=True, side="buy", pair="XBTUSDC", qty=1, reason="too big", stop_price=90, price=100, max_order_usd=10)
    chunks = order_commands(live=True, side="sell", pair="XBTUSDC", qty=0.25, reason="close", stop_price=None, price=100, max_order_usd=10)
    sells = [cmd for cmd in chunks if cmd[2] == "sell"]
    assert len(sells) == 3
    assert all(float(cmd[4]) * 100 <= 10 + 1e-8 for cmd in sells)
    assert kraken_pair("ETH/USDC") == "ETHUSDC"
    assert kraken_pair("SOL/USD") == "SOLUSD"


def test_futures_parsers_and_recommendation_rule():
    now = datetime(2026, 1, 3, tzinfo=UTC)
    bars, extra = parse_kraken_futures_candles(
        {
            "candles": [
                {"time": 1767225600000, "open": "100", "high": "110", "low": "90", "close": "105", "volume": "1"},
                {"time": int(now.timestamp() * 1000), "open": "105", "high": "106", "low": "104", "close": "105", "volume": "1"},
            ]
        },
        "1d",
        now,
    )
    assert len(bars) == 1
    assert extra["dropped_unclosed"] == 1
    events = parse_kraken_funding_rates(
        {"result": "success", "rates": [{"timestamp": "2026-01-01T00:00:00Z", "relativeFundingRate": 0.00001, "fundingRate": 1}]}
    )
    assert events[0].rate == pytest.approx(1e-5)
    assert choose_row(
        [
            {"id": "fast", "round_trips": 4, "final_equity": 12, "capital": 10, "survived": True, "max_drawdown": 0.2},
            {"id": "calm", "round_trips": 4, "final_equity": 10.2, "capital": 10, "survived": True, "max_drawdown": 0.05},
            {"id": "dead", "round_trips": 6, "final_equity": 11, "capital": 10, "survived": False, "max_drawdown": 0.01},
        ]
    )["id"] == "calm"
    assert cagr(10, 0, 100) is None


def test_rsi_macd_short_is_opt_in():
    closes = [100.0] * 40
    long_only, _ind, _warn = compute_signals("rsi_macd", closes, None, False)
    both, indicators, _warn = compute_signals("rsi_macd", closes, None, True)
    assert "_indicator_exit" not in indicators
    assert all(value >= 0 for value in long_only)
    assert len(both) == len(closes)
