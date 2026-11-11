from datetime import datetime, timedelta, timezone

import pytest

from ymzz.engine import (
    Bar,
    CostConfig,
    EngineConfig,
    Funding,
    decide_exit,
    enforce_leverage,
    liquidation_price,
    run_backtest,
    total_slip_bps,
)
from ymzz.errors import ConfigError, LeverageError

UTC = timezone.utc


def bar(i, open_, high, low, close, quote=None):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
    return Bar(start, start + timedelta(days=1), open_, high, low, close, 1.0, quote, True)


def test_leverage_allow_list_and_spot_cap():
    assert enforce_leverage(1, "spot") == 1
    assert enforce_leverage(1.5, "perpetual") == 1.5
    assert enforce_leverage(2, "perpetual") == 2
    with pytest.raises(LeverageError):
        enforce_leverage(2.01, "perpetual")
    with pytest.raises(LeverageError):
        enforce_leverage(3, "perpetual")
    with pytest.raises(LeverageError):
        enforce_leverage(1.2, "perpetual")
    with pytest.raises(LeverageError):
        enforce_leverage(1.5, "spot")
    with pytest.raises(LeverageError):
        enforce_leverage(2, "spot")
    with pytest.raises(LeverageError):
        enforce_leverage("2", "perpetual")


def test_liquidation_price_and_not_naive_multiple():
    price = liquidation_price(100, 0.2, 10, 0.004, 1)
    assert price == pytest.approx(50.200803)
    short = liquidation_price(100, 0.2, 10, 0.004, -1)
    assert short == pytest.approx(100 * 1.5 / 1.004)
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 40, 40)]
    result = run_backtest(
        bars,
        [1, 0],
        [],
        EngineConfig(
            capital=10,
            leverage=2,
            instrument="perpetual",
            cost=CostConfig(fee_bps=0, slippage_bps=0),
            tp_pct=None,
            sl_pct=None,
            mmr=0.004,
            max_order_usd=50,
        ),
    )
    exits = [row for row in result.trades if row["action"] == "sell"]
    assert exits[-1]["reason"] == "liquidation"
    assert exits[-1]["price"] == pytest.approx(price)
    naive = 10 * (1 + 2 * (40 / 100 - 1))
    assert result.metrics["final_equity"] == pytest.approx(0.0401606, rel=1e-4)
    assert abs(result.metrics["final_equity"] - naive) > 1
    assert result.metrics["final_equity"] < 1


def test_gap_through_bankruptcy_floors_at_zero():
    # 先在 100 成交，下一根開盤跳到 30。跳空穿過清算價與破產價，權益歸零且沒有補錢。
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 100, 100), bar(2, 30, 30, 30, 30)]
    result = run_backtest(
        bars,
        [1, 1, 1],
        [],
        EngineConfig(
            capital=10,
            leverage=2,
            instrument="perpetual",
            cost=CostConfig(0, 0),
            tp_pct=None,
            sl_pct=None,
            max_order_usd=50,
        ),
    )
    assert result.metrics["final_equity"] == 0
    assert result.metrics["liquidations"] == 1
    assert result.trades[-1]["reason"] == "liquidation"
    assert result.trades[-1]["price"] == pytest.approx(30)
    assert any("歸零" in item for item in result.warnings)


def test_fee_and_slippage_not_flat_when_price_unchanged():
    bars = [bar(i, 100, 100, 100, 100, quote=1_000_000) for i in range(3)]
    result = run_backtest(
        bars,
        [1, 0, 0],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(10, 0), tp_pct=None, sl_pct=None),
    )
    assert result.metrics["final_equity"] < 10
    assert result.metrics["fees_paid"] == pytest.approx(10 - result.metrics["final_equity"])
    slipped = run_backtest(
        bars,
        [1, 0, 0],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 100), tp_pct=None, sl_pct=None),
    )
    buy = next(row for row in slipped.trades if row["action"] == "buy")
    sell = next(row for row in slipped.trades if row["action"] == "sell")
    assert buy["price"] == pytest.approx(101)
    assert sell["price"] == pytest.approx(99)


def test_volume_impact_changes_slip_and_refuses_missing_quote():
    cost = CostConfig(fee_bps=0, slippage_bps=5, impact_coef=0.1)
    assert total_slip_bps(1000, 100_000, cost) == pytest.approx(15)
    with pytest.raises(ConfigError):
        total_slip_bps(1000, None, cost)


def test_same_bar_stop_before_take_profit():
    choice = decide_exit(1, 100, 120, 90, 95, 110, None)
    assert choice == ("stop_loss", 95)
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 120, 90, 100)]
    result = run_backtest(
        bars,
        [1, 0],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=0.10, sl_pct=0.05),
    )
    assert result.trades[-1]["reason"] == "stop_loss"
    assert result.trades[-1]["price"] == pytest.approx(95)


def test_gap_open_fills_at_open_not_stop():
    choice = decide_exit(1, 80, 80, 70, 95, 110, None)
    assert choice == ("stop_loss", 80)


def test_take_profit_when_stop_not_touched():
    choice = decide_exit(1, 100, 120, 96, 95, 110, None)
    assert choice == ("take_profit", 110)


def test_tighter_stop_hits_before_liquidation():
    choice = decide_exit(1, 100, 100, 40, 90, None, 50)
    assert choice[0] == "stop_loss"
    choice = decide_exit(1, 100, 100, 30, 40, None, 50)
    assert choice[0] == "liquidation"


def test_next_bar_fill_not_signal_close():
    bars = [bar(0, 100, 100, 100, 100), bar(1, 110, 110, 110, 110)]
    result = run_backtest(
        bars,
        [1, 1],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None),
    )
    assert result.trades[0]["price"] == pytest.approx(110)
    assert len(bars) == 2
    assert result.metrics["open_qty"] > 0


def test_one_bar_does_not_trade():
    result = run_backtest(
        [bar(0, 100, 100, 100, 100)],
        [1],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None),
    )
    assert result.trades == []


def test_funding_paid_by_long_and_skipped_if_stopped():
    start = datetime(2026, 1, 2, tzinfo=UTC)
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 100, 100)]
    event = Funding(start + timedelta(hours=12), 0.001)
    held = run_backtest(
        bars,
        [1, 1],
        [event],
        EngineConfig(capital=10, leverage=1, instrument="perpetual", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None),
    )
    funding_rows = [row for row in held.trades if row["action"] == "funding"]
    assert len(funding_rows) == 1
    qty = held.metrics["open_qty"]
    assert funding_rows[0]["fee"] == pytest.approx(qty * 100 * 0.001)
    stopped = run_backtest(
        [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 80, 80)],
        [1, 1],
        [event],
        EngineConfig(capital=10, leverage=1, instrument="perpetual", cost=CostConfig(0, 0), tp_pct=None, sl_pct=0.05),
    )
    assert not any(row["action"] == "funding" for row in stopped.trades)


def test_drawdown_and_lot_block():
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 50, 50)]
    result = run_backtest(
        bars,
        [1, 1],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None),
    )
    assert result.metrics["max_drawdown"] == pytest.approx(0.5)
    blocked = run_backtest(
        bars,
        [1, 1],
        [],
        EngineConfig(capital=10, leverage=1, instrument="spot", cost=CostConfig(0, 0), tp_pct=None, sl_pct=None, lot_size=100),
    )
    assert blocked.metrics["trade_count"] == 0
    assert blocked.metrics["entries_blocked_by_lot"] >= 1


def test_unclosed_bar_rejected():
    bad = bar(0, 1, 1, 1, 1)
    bad.closed = False
    with pytest.raises(ConfigError):
        run_backtest([bad], [0], [], EngineConfig(10, 1, "spot"))
