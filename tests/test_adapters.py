import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ymzz.data import (
    StockAppAdapter,
    parse_binance_klines,
    parse_eastmoney,
    parse_gecko_ohlcv,
    parse_kraken_ohlc,
    parse_okx_candles,
    parse_polymarket_history,
    parse_sina,
    parse_tencent,
)
from ymzz.errors import DataSourceError, StockAppUnidentified
from ymzz.http_cache import DataClient

ROOT = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 10, 10, 51, tzinfo=timezone.utc)


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_recorded_sina_tencent_eastmoney_units():
    sina, meta = parse_sina(load("sina_sh600519_recorded.json"), NOW)
    assert meta["dropped_unclosed"] == 0
    assert sina[-1].close == pytest.approx(1439)
    assert sina[-1].volume == pytest.approx(2303784)
    early = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
    sina_open, meta_open = parse_sina(load("sina_sh600519_recorded.json"), early)
    assert len(sina_open) == len(sina) - 1
    assert meta_open["dropped_unclosed"] == 1

    tencent, tmeta = parse_tencent(load("tencent_sh600519_recorded.json"), "sh600519", NOW)
    assert tencent[-1].volume == pytest.approx(23038 * 100)
    assert tmeta["adjust"] == "qfq"

    east, emeta = parse_eastmoney(load("eastmoney_600519_recorded.json"), NOW)
    assert east[-1].volume == pytest.approx(23038 * 100)
    assert east[-1].quote_volume == pytest.approx(3316078849)
    assert emeta["adjust"] == "qfq"


def test_recorded_okx_drops_unclosed_and_kraken_gecko():
    bars, meta = parse_okx_candles(load("okx_btc_spot_recorded.json"), "1d", NOW)
    assert bars == []
    assert meta["dropped_unclosed"] == 1
    swap, smeta = parse_okx_candles(load("okx_btc_swap_recorded.json"), "1d", NOW)
    assert len(swap) == 1
    assert smeta["dropped_unclosed"] == 1
    assert swap[0].close == pytest.approx(75492.9)
    gecko, gmeta = parse_gecko_ohlcv(load("gecko_sol_usdc_recorded.json"), "1d", NOW)
    assert gmeta["dropped_unclosed"] == 1
    assert len(gecko) == 2
    kraken, kmeta = parse_kraken_ohlc(load("kraken_xbtusd_recorded_tail.json"), "1d", NOW)
    assert kmeta["dropped_unclosed"] == 1
    assert len(kraken) == 1


def test_binance_451_and_shape_do_not_invent_bars():
    payload = load("binance_451_recorded.json")
    with pytest.raises(DataSourceError):
        parse_binance_klines(payload, "1d", NOW)
    shape = [[1700000000000, "100", "110", "90", "105", "1", 1700086399999, "1000", 1, "0", "0", "0"]]
    future = datetime(2023, 11, 14, tzinfo=timezone.utc)
    empty, meta = parse_binance_klines(shape, "1d", future)
    assert empty == []
    assert meta["dropped_unclosed"] == 1
    later = datetime(2023, 11, 16, tzinfo=timezone.utc)
    bars, _ = parse_binance_klines(shape, "1d", later)
    assert len(bars) == 1
    assert bars[0].quote_volume == pytest.approx(1000)


def test_polymarket_recorded_points_and_empty():
    bars, meta = parse_polymarket_history(load("polymarket_prices_history_recorded.json"), NOW)
    assert len(bars) == 2
    assert bars[0].high == bars[0].low == bars[0].close
    assert meta["bar_kind"] == "price_point"
    with pytest.raises(DataSourceError):
        parse_polymarket_history({"history": []}, NOW)


def test_live_error_does_not_reuse_cache(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(451, text="error code: 1026")

    client = DataClient(tmp_path, http=httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _s: None)
    payload, meta = client.get_json("https://example.test/prices")
    assert payload["ok"] is True
    assert meta["fetched_at_kst"].endswith("+09:00")
    cached, _ = client.get_json("https://example.test/prices", mode="cache")
    assert cached["ok"] is True
    assert calls["n"] == 1
    with pytest.raises(DataSourceError) as caught:
        client.get_json("https://example.test/prices")
    assert caught.value.extra["status_code"] == 451
    with pytest.raises(DataSourceError):
        client.get_json("https://example.test/prices", mode="cache")
    assert calls["n"] == 2


def test_cache_miss_and_stock_app():
    client = DataClient("/tmp/ymzz_empty_cache_test", sleeper=lambda _s: None)
    with pytest.raises(DataSourceError):
        client.get_json("https://example.test/missing", mode="cache")
    with pytest.raises(StockAppUnidentified):
        StockAppAdapter().load()
