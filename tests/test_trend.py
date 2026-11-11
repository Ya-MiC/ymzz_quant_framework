"""低頻趨勢的候選與訊號。不打網路。"""

from datetime import datetime, timedelta, timezone

import pytest

from ymzz.data import merge_futures_bars
from ymzz.engine import Bar
from ymzz.errors import DataSourceError
from ymzz.trend import ATR_MIN, GRID, INDICATORS, MIN_MOVE, ROUND_TRIP_MAKER, build_signals, spec_id

UTC = timezone.utc


def _bar(i, close, high=None, low=None, hours=4):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hours * i)
    return Bar(
        start,
        start + timedelta(hours=hours),
        close,
        close if high is None else high,
        close if low is None else low,
        close,
        1.0,
        None,
        True,
    )


def test_failure_notes_keep_the_old_losses():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    failure = (root / "docs" / "failures" / "1111_1618_seoul_失敗紀錄.md").read_text(encoding="utf-8")
    report = (root / "docs" / "1111_1618_seoul_低頻趨勢費後驗證.md").read_text(encoding="utf-8")
    assert "-29.39%" in failure
    assert "-4.82%" in failure
    assert "沒有刪" in failure
    assert "RSI 12" in report or "rsi12" in report
    assert "rsi22" in report
    assert "沒有在樣本外賺到錢" in report


def test_grid_keeps_rsi_12_14_and_22_and_the_fee_multiple():
    assert ROUND_TRIP_MAKER == pytest.approx(0.008)
    assert MIN_MOVE == pytest.approx(0.024)
    for spec in GRID:
        assert spec["tp"] >= MIN_MOVE
        assert spec["sl"] < spec["tp"]
        assert len(INDICATORS[spec["family"]]) <= 3
    rsi = [spec for spec in GRID if spec["family"] == "rsi"]
    filtered = [spec for spec in GRID if spec["family"] == "rsi_regime"]
    assert {spec["period"] for spec in rsi} == {12, 14, 22}
    assert {spec["period"] for spec in filtered} == {12, 14, 22}
    assert len({spec_id(spec) for spec in GRID}) == len(GRID)


def test_donchian_uses_only_past_bars_and_skips_a_narrow_range():
    quiet = [_bar(i, 100) for i in range(30)]
    quiet.append(_bar(30, 101, high=101, low=100))
    spec = {"family": "donchian", "lookback": 5, "side": "both", "sl": 0.012, "tp": 0.024, "trend": 100}
    assert build_signals(quiet, spec, chase_bars=0)[-1] == 0.0
    wide = [_bar(i, 100, high=110, low=90) for i in range(30)]
    wide.append(_bar(30, 111, high=112, low=108))
    signals = build_signals(wide, spec, chase_bars=0)
    assert signals[-1] == 1.0
    later = wide + [_bar(31, 200, high=200, low=190)]
    again = build_signals(later, spec, chase_bars=0)
    assert again[:-1] == signals


def test_chase_and_long_only_and_regime_can_keep_the_book_flat():
    pumped = [_bar(i, 100, high=110, low=90) for i in range(20)]
    pumped.append(_bar(20, 140, high=141, low=130))
    spec = {"family": "donchian", "lookback": 5, "side": "both", "sl": 0.012, "tp": 0.024, "trend": 100}
    assert build_signals(pumped, spec, chase_bars=20)[-1] == 0.0
    short_spec = dict(spec, side="long")
    dumped = [_bar(i, 100, high=110, low=90) for i in range(20)]
    dumped.append(_bar(20, 80, high=90, low=79))
    assert build_signals(dumped, short_spec, chase_bars=0)[-1] == 0.0
    assert build_signals(dumped, spec, chase_bars=0)[-1] == -1.0
    flat = [_bar(i, 100) for i in range(120)]
    regime = {"family": "donchian_regime", "lookback": 5, "side": "both", "sl": 0.012, "tp": 0.024, "trend": 20}
    assert all(value == 0.0 for value in build_signals(flat, regime, chase_bars=0))
    assert ATR_MIN == 0.01


def test_merge_rejects_a_conflicting_bar_and_drops_a_gap():
    first = _bar(0, 100)
    second = _bar(1, 101)
    assert merge_futures_bars([second, first, first], timedelta(hours=4))[0] == [first, second]
    clash = Bar(first.open_time, first.close_time, 100, 100, 100, 999, 1.0, None, True)
    with pytest.raises(DataSourceError):
        merge_futures_bars([first, clash], timedelta(hours=4))
    jumped = _bar(5, 103)
    kept, gaps = merge_futures_bars([first, second, jumped], timedelta(hours=4))
    assert kept == [jumped]
    assert gaps[0]["before"].startswith("2026-01-01T20:00:00")
