"""Codex review regressions plus the A-share and optional-adapter checks."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ymzz.ccxt_adapter import load_ccxt_bars
from ymzz.data import _load_crypto, _load_polymarket, paginate_binance_funding
from ymzz.engine import (
    Bar,
    CostConfig,
    EngineConfig,
    apply_ashare_limits,
    ashare_price_limit_rate,
    run_backtest,
)
from ymzz.errors import ConfigError
from ymzz.external_export import export_research_csv
from ymzz.http_cache import DataClient, _LAST_HIT
from ymzz.paths import resolve_web_dir, web_dir
from ymzz.polymarket_sdk import SdkUnavailable, load_public_history
from ymzz.strategies import compute_signals
from ymzz.wallet import replay_swaps

UTC = timezone.utc


def bar(i, open_, high, low, close):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
    return Bar(start, start + timedelta(days=1), open_, high, low, close, 1.0, 1_000_000.0, True)


def _cfg(**kwargs):
    base = dict(capital=10_000, leverage=1, instrument="spot", cost=CostConfig(10, 5), tp_pct=0.10, sl_pct=0.05)
    base.update(kwargs)
    return EngineConfig(**base)


def test_binance_funding_paginates_with_start_and_end():
    calls = []

    def fetch(params):
        calls.append(dict(params))
        assert "startTime" in params and "endTime" in params
        start = int(params["startTime"])
        if start == 0:
            return [
                {"fundingTime": 0, "fundingRate": "0.0001"},
                {"fundingTime": 1000, "fundingRate": "0.0002"},
            ]
        if start == 1001:
            return [{"fundingTime": 5000, "fundingRate": "0.0009"}]
        return []

    rows, incomplete = paginate_binance_funding(fetch, 0, 9_000, limit=2)
    assert [int(row["fundingTime"]) for row in rows] == [0, 1000, 5000]
    assert incomplete is False
    assert len(calls) == 2


def test_binance_loader_requests_the_bar_window(monkeypatch):
    open_ms = 1_700_000_000_000
    close_ms = open_ms + 86_400_000 - 1
    kline = [open_ms, "100", "110", "90", "105", "1", close_ms, "1000", 1, 0, 0, 0]
    calls = []

    class Client:
        def get_json(self, url, params=None, mode="live", headers=None):
            calls.append((url, dict(params or {})))
            if "klines" in url:
                return [kline], {"sha256": "k"}
            start = int(params["startTime"])
            if start == open_ms:
                return [{"fundingTime": open_ms + i, "fundingRate": "0.0001"} for i in range(1000)], {"sha256": "p1"}
            if start == open_ms + 1000:
                return [{"fundingTime": open_ms + 1000, "fundingRate": "0.0002"}], {"sha256": "p2"}
            return [], {"sha256": "empty"}

    now = datetime.fromtimestamp((close_ms + 60_000) / 1000, tz=UTC)
    _bars, funding, extra, _prov = _load_crypto(
        "binance", "BTC/USDT", "1d", "perpetual", None, None, Client(), now, "live"
    )
    fund_calls = [params for url, params in calls if "fundingRate" in url]
    assert fund_calls
    assert all("startTime" in params and "endTime" in params for params in fund_calls)
    assert len(fund_calls) >= 2
    assert any(int(item.time.timestamp() * 1000) == open_ms for item in funding)
    assert extra["funding_points"] == 1001


def test_wallet_replay_sorts_newest_first_rpc_order():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    later = start + timedelta(hours=2)
    events = [
        {
            "kind": "swap",
            "pnl_relevant": True,
            "time": later.isoformat(),
            "signature": "sell",
            "paid_mint": "TOKEN",
            "received_mint": "USDC",
            "paid_qty": 1,
            "received_qty": 110,
        },
        {
            "kind": "swap",
            "pnl_relevant": True,
            "time": start.isoformat(),
            "signature": "buy",
            "received_mint": "TOKEN",
            "paid_mint": "USDC",
            "received_qty": 1,
            "paid_qty": 100,
        },
    ]
    bars = [
        Bar(start, start + timedelta(hours=1), 100, 100, 100, 100, closed=True),
        Bar(later, later + timedelta(hours=1), 110, 110, 110, 110, closed=True),
    ]
    result = replay_swaps(events, bars, delay_seconds=0, slippage_bps=0, capital=1000, base_mint="TOKEN")
    filled = [row for row in result["rows"] if row["status"] == "filled"]
    assert [row["signature"] for row in filled] == ["buy", "sell"]
    assert result["base_qty"] == pytest.approx(0)


def test_edge_trigger_does_not_reenter_until_signal_changes():
    bars = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 100, 80, 90),
        bar(2, 100, 101, 99, 100),
        bar(3, 100, 101, 99, 100),
    ]
    edge = run_backtest(bars, [1, 1, 1, 1], [], _cfg())
    assert sum(1 for row in edge.trades if row["action"] == "buy") == 1
    level = run_backtest(bars, [1, 1, 1, 1], [], _cfg(entry_trigger="level"))
    assert sum(1 for row in level.trades if row["action"] == "buy") == 2
    fresh = run_backtest(bars, [1, 0, 1, 0], [], _cfg())
    assert sum(1 for row in fresh.trades if row["action"] == "buy") == 2


def test_rsi_macd_pulse_waits_for_a_new_entry_pulse():
    bars = [bar(i, 100, 100, 100, 100) for i in range(5)]
    bars[1] = bar(1, 100, 100, 80, 90)
    result = run_backtest(bars, [1, 0, 0, 1, 0], [], _cfg(signal_mode="pulse"))
    buys = [row for row in result.trades if row["action"] == "buy"]
    assert len(buys) == 2
    assert buys[0]["time"].startswith("2026-01-02")
    assert buys[1]["time"].startswith("2026-01-05")


def test_fractional_period_is_rejected():
    closes = [float(100 + i) for i in range(40)]
    with pytest.raises(ConfigError) as exc:
        compute_signals("rsi_single", closes, {"period": 14.9})
    assert "14" in exc.value.message_zh
    assert "不會" in exc.value.message_zh
    assert "截" in exc.value.message_zh
    with pytest.raises(ConfigError) as grids:
        compute_signals("grid", closes, {"n_grids": 5.9})
    assert "不會" in grids.value.message_zh
    compute_signals("rsi_single", closes, {"period": 14})
    compute_signals("rsi_macd", closes, {"rsi_period": 14.0, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9})


def test_rate_limiter_reserves_the_next_slot():
    _LAST_HIT.clear()
    sleeps = []

    def clock():
        return 100.0

    def sleeper(wait):
        sleeps.append(wait)

    def handler(_request):
        return httpx.Response(200, json={"ok": True})

    client = DataClient(
        "/tmp/ymzz-pace-test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=0.25,
        sleeper=sleeper,
        clock=clock,
    )
    barrier = threading.Barrier(2)

    def hit():
        barrier.wait()
        client.get_json("https://pace.example/v1", {"n": threading.get_ident()})

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sleeps == [0.25]


def test_web_dir_uses_packaged_files_when_checkout_is_absent(tmp_path):
    packaged = tmp_path / "ymzz" / "web"
    packaged.mkdir(parents=True)
    (packaged / "index.html").write_text("<html>installed</html>", encoding="utf-8")
    found = resolve_web_dir([tmp_path / "missing" / "web"], packaged)
    assert found == packaged
    assert (web_dir() / "index.html").is_file()
    assert (web_dir() / "dashboard.html").is_file()
    packaged_here = Path(__file__).resolve().parents[1] / "src" / "ymzz" / "web"
    assert (resolve_web_dir([], packaged_here) / "dashboard.html").is_file()


def test_ashare_limits_by_board_and_engine_blocks():
    assert ashare_price_limit_rate("600519") == 0.10
    assert ashare_price_limit_rate("300750") == 0.20
    assert ashare_price_limit_rate("688981") == 0.20
    assert ashare_price_limit_rate("600519", st=True) == 0.05
    assert ashare_price_limit_rate("ST600000") == 0.05
    stamped = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 10, 10)]
    apply_ashare_limits(stamped, 0.10)
    assert stamped[1].limit_up == 11
    assert stamped[1].limit_down == 9
    star = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 10, 10)]
    apply_ashare_limits(star, ashare_price_limit_rate("688981"))
    assert star[1].limit_up == 12
    st_bars = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 10, 10)]
    apply_ashare_limits(st_bars, 0.05)
    assert st_bars[1].limit_up == 10.5
    assert st_bars[1].limit_down == 9.5

    locked = [bar(0, 10, 10, 10, 10), bar(1, 11, 11, 11, 11)]
    locked[1].limit_up = 11
    locked[1].limit_down = 9
    blocked_buy = run_backtest(locked, [1, 1], [], _cfg(lot_size=100, capital=100_000))
    assert not any(row["action"] == "buy" for row in blocked_buy.trades)
    assert any(row["reason"] == "limit_up" for row in blocked_buy.skips)

    down = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 10, 10), bar(2, 9, 9, 9, 9)]
    down[2].limit_up = 11
    down[2].limit_down = 9
    blocked_sell = run_backtest(down, [1, 0, 0], [], _cfg(lot_size=100, capital=100_000, sl_pct=0.05, tp_pct=0.10))
    assert any(row["action"] == "buy" for row in blocked_sell.trades)
    assert not any(row["action"] == "sell" for row in blocked_sell.trades)
    assert any(row["reason"] == "limit_down" for row in blocked_sell.skips)


def test_ashare_t_plus_one_and_suspension():
    bars = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 8, 9), bar(2, 9, 10, 9, 9.5)]
    held = run_backtest(bars, [1, 0, 0], [], _cfg(lot_size=100, capital=100_000, t_plus_one=True))
    assert not any(row["reason"] == "stop_loss" for row in held.trades)
    sells = [row for row in held.trades if row["action"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["time"].startswith("2026-01-03")
    stopped = run_backtest(bars, [1, 0, 0], [], _cfg(lot_size=100, capital=100_000, t_plus_one=False))
    assert any(row["reason"] == "stop_loss" for row in stopped.trades)

    halted = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 8, 9)]
    halted[1].suspended = True
    quiet = run_backtest(halted, [1, 0], [], _cfg(lot_size=100, capital=100_000, t_plus_one=True))
    assert not any(row["action"] in {"buy", "sell"} for row in quiet.trades)
    assert any(row["reason"] == "suspended" for row in quiet.skips)

    carried = [bar(0, 10, 10, 10, 10), bar(1, 10, 10, 10, 10), bar(2, 8, 8, 1, 8)]
    carried[2].suspended = True
    still = run_backtest(carried, [1, 1, 1], [], _cfg(lot_size=100, capital=100_000, sl_pct=0.05, tp_pct=0.10))
    assert any(row["action"] == "buy" for row in still.trades)
    assert not any(row["action"] == "sell" for row in still.trades)
    assert still.metrics["open_qty"] > 0


def test_polymarket_sdk_and_httpx_fallback(monkeypatch):
    class Point:
        def __init__(self, timestamp, price):
            self.timestamp = timestamp
            self.price = price

    class Page:
        items = (Point(1_700_000_000, "0.42"),)

    class Pages:
        def first_page(self):
            return Page()

    class Market:
        question = "beta sample"
        outcomes = type("O", (), {"yes": type("Y", (), {"token_id": "token-1", "name": "Yes"})()})()

    class Sdk:
        def get_market(self, **kwargs):
            assert kwargs == {"id": "559651"}
            return Market()

        def list_price_history(self, **kwargs):
            assert kwargs["asset_id"] == "token-1"
            return Pages()

    history, meta = load_public_history("559651", client=Sdk())
    assert history["history"][0]["p"] == 0.42
    assert meta["beta"] is True
    assert meta["adapter"] == "polymarket-sdk"

    def missing(_symbol, client=None):
        raise SdkUnavailable("polymarket-client 沒有安裝")

    monkeypatch.setattr("ymzz.polymarket_sdk.load_public_history", missing)

    class Client:
        def get_json(self, url, params=None, mode="live", headers=None):
            if "gamma-api" in url:
                return {"clobTokenIds": ["tok"], "outcomes": ["Yes"], "question": "q"}, {"sha256": "g"}
            return {"history": [{"t": 1_700_000_000, "p": 0.4}]}, {"sha256": "h"}

    now = datetime(2026, 1, 2, tzinfo=UTC)
    bars, extra, prov = _load_polymarket("559651", None, None, Client(), now, "live", adapter="sdk")
    assert extra["adapter"] == "httpx-fallback"
    assert extra["beta"] is True
    assert bars[-1].close == pytest.approx(0.4)
    assert prov["sha256"] == "h"


def test_ccxt_adapter_is_optional(monkeypatch):
    import ymzz.ccxt_adapter as adapter

    original = adapter._ccxt_fetch

    def fake(exchange_id, symbol, timeframe):
        assert exchange_id == "kraken"
        assert symbol == "BTC/USD"
        return [[1_700_000_000_000, 100, 110, 90, 105, 2]]

    monkeypatch.setattr(adapter, "_ccxt_fetch", fake)
    bars, extra = load_ccxt_bars("kraken", "BTC/USD", "1d", datetime(2026, 1, 2, tzinfo=UTC))
    assert bars[0].close == 105
    assert extra["adapter"] == "ccxt"
    monkeypatch.setattr(adapter, "_ccxt_fetch", original)

    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "ccxt":
            raise ImportError("no ccxt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ConfigError) as exc:
        load_ccxt_bars("okx", "BTC/USDT", "1d", datetime(2026, 1, 2, tzinfo=UTC))
    assert "沒有安裝" in exc.value.message_zh


def test_export_ohlcv_and_signals(tmp_path):
    bars = [bar(0, 1, 2, 0.5, 1.5)]
    paths = export_research_csv(bars, [1], tmp_path)
    text = Path(paths["ohlcv"]).read_text(encoding="utf-8")
    assert "time,open,high,low,close,volume" in text
    assert "1.5" in text
    assert "time,signal" in Path(paths["signals"]).read_text(encoding="utf-8")
