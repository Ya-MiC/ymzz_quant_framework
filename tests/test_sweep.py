"""追高濾網、Kraken 三個現貨代碼，以及掃描推薦規則。不打網路。"""

from datetime import datetime, timedelta, timezone

import pytest

from ymzz.data import _load_crypto, kraken_rest_pair
from ymzz.engine import Bar, CostConfig, EngineConfig, run_backtest
from ymzz.errors import ConfigError
from ymzz.paper import kraken_pair
from ymzz.strategies import STRATEGIES
from ymzz.sweep import SWEEP_STRATEGIES, pick_recommendation

UTC = timezone.utc


def _day(i, price):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
    return Bar(start, start + timedelta(days=1), price, price, price, price, 1.0, 1_000_000.0, True)


def test_kraken_spot_pairs_and_since():
    assert kraken_rest_pair("ETH/USD") == "ETHUSD"
    assert kraken_rest_pair("SOL/USD") == "SOLUSD"
    assert kraken_rest_pair("BTC/USD") == "XBTUSD"
    assert kraken_rest_pair("BTC/USDC") == "XBTUSDC"
    assert kraken_rest_pair("ETH/USDC") == "ETHUSDC"
    assert kraken_rest_pair("SOL/USDC") == "SOLUSDC"
    assert kraken_pair("ETH/USD") == "ETHUSD"
    assert kraken_pair("BTC/USDC") == "XBTUSDC"
    assert kraken_pair("SOL/USDC") == "SOLUSDC"
    with pytest.raises(ConfigError):
        kraken_rest_pair("DOGE/USD")
    with pytest.raises(ConfigError):
        kraken_pair("DOGE/USD")
    calls = []

    class Client:
        def get_json(self, url, params=None, mode="live", headers=None):
            calls.append(dict(params))
            opened = int(datetime(2025, 11, 11, tzinfo=UTC).timestamp())
            row = [opened, "100", "110", "90", "105", "0", "1", 1]
            return {"error": [], "result": {"XETHZUSD": [row], "last": opened}}, {"sha256": "x"}

    start = datetime(2025, 11, 11, tzinfo=UTC)
    now = datetime(2026, 11, 11, tzinfo=UTC)
    bars, _funding, _extra, _prov = _load_crypto("kraken", "ETH/USD", "1d", "spot", start, None, Client(), now, "live")
    assert calls[0]["pair"] == "ETHUSD"
    assert calls[0]["since"] == str(int(start.timestamp()))
    assert bars


def test_chase_filter_skips_a_pump_and_keeps_a_quiet_entry():
    quiet = [_day(i, 100) for i in range(22)]
    signals = [0.0] * 22
    signals[20] = 1.0
    held = run_backtest(quiet, signals, [], EngineConfig(capital=1000, leverage=1, instrument="spot", cost=CostConfig(10, 5), tp_pct=0.1, sl_pct=0.05, chase_rise=0.30, chase_bars=20))
    assert any(row["action"] == "buy" for row in held.trades)
    pumped = [_day(i, 100) for i in range(21)]
    pumped.append(Bar(datetime(2026, 1, 22, tzinfo=UTC), datetime(2026, 1, 23, tzinfo=UTC), 140, 150, 139, 145, 1.0, 1_000_000.0, True))
    blocked = run_backtest(pumped, signals, [], EngineConfig(capital=1000, leverage=1, instrument="spot", cost=CostConfig(10, 5), tp_pct=0.1, sl_pct=0.05, chase_rise=0.30, chase_bars=20))
    assert not any(row["action"] == "buy" for row in blocked.trades)
    assert any(row["reason"] == "chase_filter" for row in blocked.skips)
    assert blocked.metrics["skips_chase"] == 1


def test_sweep_strategies_stay_within_three_indicators():
    for strategy_id in SWEEP_STRATEGIES:
        assert len(STRATEGIES[strategy_id].decision_indicators) <= 3


def _row(name, dd, ret, equity, trips=6, chase=False):
    return {
        "id": name,
        "chase": chase,
        "max_drawdown": dd,
        "total_return": ret,
        "final_equity": equity,
        "capital": 10,
        "round_trips": trips,
    }


def test_recommendation_prefers_capital_and_drawdown():
    rows = [
        _row("BTC/USD|1d|macd", 0.40, 0.50, 15),
        _row("ETH/USD|1d|rsi_single", 0.04, 0.02, 10.2),
        _row("SOL/USD|4h|bollinger", 0.01, 0.80, 9.0, trips=2),
    ]
    picked = pick_recommendation(rows)
    assert picked["id"] == "ETH/USD|1d|rsi_single"
    with_filter = rows + [_row("ETH/USD|1d|rsi_single|+chase", 0.03, 0.01, 10.3, chase=True)]
    assert pick_recommendation(with_filter)["id"] == "ETH/USD|1d|rsi_single|+chase"
    thin = [_row("BTC/USD|1d|macd", 0.01, 0.2, 12, trips=1)]
    assert pick_recommendation(thin)["id"] is None
    same = [
        _row("BTC/USD|1d|rsi_macd", 0.06, 0.14, 11.4),
        _row("BTC/USD|1d|rsi_macd|+chase", 0.06, 0.14, 11.4, chase=True),
    ]
    same[1]["skips_chase"] = 0
    assert "沒有擋下" in pick_recommendation(same)["reason_zh"]


def test_chase_filter_does_not_flatten_an_open_position():
    bars = [_day(i, 100) for i in range(22)]
    bars.append(Bar(datetime(2026, 1, 23, tzinfo=UTC), datetime(2026, 1, 24, tzinfo=UTC), 140, 141, 139, 140, 1.0, 1_000_000.0, True))
    result = run_backtest(
        bars,
        [1.0] * len(bars),
        [],
        EngineConfig(capital=1000, leverage=1, instrument="spot", cost=CostConfig(10, 5), tp_pct=0.5, sl_pct=0.5, chase_rise=0.30, chase_bars=20),
    )
    assert any(row["action"] == "buy" for row in result.trades)
    assert not any(row["action"] == "sell" for row in result.trades)
    assert result.metrics["skips_chase"] == 0
