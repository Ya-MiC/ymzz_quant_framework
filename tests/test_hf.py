"""高頻觸發的成交規則。不打網路。"""

from datetime import datetime, timedelta, timezone

import pytest

from ymzz.cli import build_parser
from ymzz.engine import Bar
from ymzz.errors import ConfigError
from ymzz.hf import HfConfig, apply_hf_preset, choose_hf, preset_config, run_hf, step_hf, trigger_signals
from ymzz.paper import LiveRefused

UTC = timezone.utc


def _bar(i, close, high=None, low=None, open_=None, minutes=60):
    start = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(minutes=minutes * i)
    opened = close if open_ is None else open_
    return Bar(
        start,
        start + timedelta(minutes=minutes),
        opened,
        close if high is None else high,
        close if low is None else low,
        close,
        1.0,
        None,
        True,
    )


def _cfg(**overrides):
    raw = dict(
        capital=10,
        leverage=1,
        max_margin_usd=10,
        max_notional_usd=10,
        maker_bps=0,
        taker_bps=0,
        slip_bps=0,
        sl=0.05,
        tp=0.10,
        expire=4,
        daily_loss=0.15,
        global_dd=0.50,
        entry="maker",
    )
    raw.update(overrides)
    return HfConfig(**raw)


def test_limit_fills_only_when_price_trades_through_and_stop_beats_target():
    signals = trigger_signals([100, 100, 100], 2, 30, 70)
    assert signals == [0.0, 0.0, 0.0]
    bars = [
        _bar(0, 100),
        _bar(1, 100, open_=100, high=101, low=100),
        _bar(2, 100, open_=101, high=101, low=99),
    ]
    missed = run_hf(bars[:2], [1.0, 0.0], _cfg())
    assert missed["entries"] == 0
    filled = run_hf(bars, [1.0, 0.0, 0.0], _cfg())
    assert filled["entries"] == 1
    assert filled["open_qty"] > 0
    assert filled["max_entry_notional"] == pytest.approx(10, abs=0.05)

    stop_first = run_hf(
        [
            _bar(0, 100),
            _bar(1, 100, open_=101, high=101, low=99),
            _bar(2, 100, open_=100, high=120, low=90),
        ],
        [1.0, 0.0, 0.0],
        _cfg(sl=0.02, tp=0.05),
    )
    assert stop_first["round_trips"] == 1
    assert stop_first["trades"][0]["reason"] == "stop"
    assert stop_first["takes"] == 0


def test_gap_through_a_limit_does_not_count_as_a_maker_fill():
    result = run_hf(
        [_bar(0, 100), _bar(1, 90, high=95, low=80)],
        [1.0, 0.0],
        _cfg(),
    )
    assert result["entries"] == 0
    assert result["missed_limits"] == 0 or result["open_qty"] == 0


def test_short_stop_and_notional_stay_inside_the_cap():
    shorted = run_hf(
        [
            _bar(0, 100),
            _bar(1, 99, high=101, low=98),
            _bar(2, 110, high=112, low=108),
        ],
        [-1.0, 0.0, 0.0],
        _cfg(sl=0.05, tp=0.10),
    )
    assert shorted["trades"][0]["side"] == "short"
    assert shorted["trades"][0]["reason"] == "stop"
    big = run_hf(
        [_bar(0, 100), _bar(1, 100, open_=101, high=101, low=99)],
        [1.0, 0.0],
        _cfg(capital=1000, max_margin_usd=10, max_notional_usd=10),
    )
    assert big["max_entry_notional"] == pytest.approx(10, abs=0.05)
    levered = run_hf(
        [_bar(0, 100), _bar(1, 100, open_=101, high=101, low=99)],
        [1.0, 0.0],
        _cfg(capital=1000, leverage=2, max_margin_usd=10, max_notional_usd=20),
    )
    assert levered["max_entry_notional"] == pytest.approx(20, abs=0.05)


def test_daily_and_global_breakers_block_the_next_entry():
    bars = [
        _bar(0, 100),
        _bar(1, 100, open_=101, high=101, low=99),
        _bar(2, 80, open_=80, high=80, low=70),
        _bar(3, 100, open_=101, high=101, low=99),
    ]
    halted = run_hf(bars, [1.0, 0.0, 0.0, 1.0], _cfg(global_dd=0.99, daily_loss=0.15, sl=0.05))
    assert halted["entries"] == 1
    assert halted["halt_skips"] >= 1
    global_halt = run_hf(bars, [1.0, 0.0, 0.0, 1.0], _cfg(global_dd=0.15, daily_loss=0.99, sl=0.05))
    assert global_halt["global_halt"] is True
    assert global_halt["entries"] == 1


def test_maker_fee_is_charged_on_the_limit_and_taker_on_the_stop():
    result = run_hf(
        [
            _bar(0, 100),
            _bar(1, 100, open_=101, high=101, low=99),
            _bar(2, 100, open_=100, high=100, low=90),
        ],
        [1.0, 0.0, 0.0],
        _cfg(maker_bps=40, taker_bps=80, slip_bps=5, sl=0.02),
    )
    assert result["round_trips"] == 1
    assert result["fees_maker"] > 0
    assert result["fees_taker"] > 0
    assert result["fees_paid"] == pytest.approx(result["fees_maker"] + result["fees_taker"])


def test_contract_tick_can_skip_a_ten_dollar_order():
    skipped = run_hf(
        [_bar(0, 150_000), _bar(1, 150_000, open_=150_100, high=150_100, low=149_000)],
        [1.0, 0.0],
        _cfg(lot_decimals=4),
    )
    assert skipped["entries"] == 0
    assert skipped["skipped_lot"] == 1


def test_paper_step_is_idempotent_and_live_is_refused(tmp_path):
    bars = [_bar(0, 100), _bar(1, 100, high=101, low=99)]
    path = tmp_path / "hf_state.json"
    cfg = _cfg()
    first = step_hf(bars=bars, signals=[1.0, 0.0], state_path=path, cfg=cfg, live=False)
    second = step_hf(bars=bars, signals=[1.0, 0.0], state_path=path, cfg=cfg, live=False)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["action"] == first["action"]
    with pytest.raises(LiveRefused):
        step_hf(bars=bars, signals=[1.0, 0.0], state_path=path, cfg=cfg, live=True)
    with pytest.raises(ConfigError):
        step_hf(bars=bars, signals=[1.0, 0.0], state_path=tmp_path / "other.json", cfg=_cfg(leverage=2), live=False)


def test_preset_wires_the_existing_runner_and_refuses_live():
    parser = build_parser()
    args = parser.parse_args(["run", "--preset", "hf", "--once"])
    apply_hf_preset(args)
    assert args.strategy == "hf_trigger"
    assert args.timeframe == "1h"
    assert args.symbol == "BTC/USD"
    assert args.capital == 10
    assert args.sl > 0 and args.tp > 0
    paper = parser.parse_args(["paper", "--preset", "hf"])
    apply_hf_preset(paper)
    assert paper.source == "kraken"
    cfg = preset_config()
    assert cfg.max_notional_usd == 10
    assert cfg.leverage == 1
    from ymzz.cli import _prepare

    live = parser.parse_args(["run", "--preset", "hf", "--mode", "live", "--once"])
    with pytest.raises(LiveRefused):
        _prepare(live)


def test_preset_matches_the_frozen_train_choice():
    import json
    from pathlib import Path

    from ymzz.hf import PRESET

    payload = json.loads((Path(__file__).resolve().parents[1] / "results" / "hf_month.json").read_text(encoding="utf-8"))
    chosen = payload["selected_on_train"]
    assert payload["selection_tag"] == "train_lost"
    assert PRESET["period"] == chosen["period"]
    assert PRESET["oversold"] == chosen["oversold"]
    assert PRESET["overbought"] == chosen["overbought"]
    assert PRESET["sl"] == chosen["sl"]
    assert PRESET["tp"] == chosen["tp"]
    assert PRESET["expire"] == chosen["expire"]
    headline = next(row for row in payload["rows"] if row["id"] == payload["headline_id"])
    assert headline["total_return"] < 0
    assert headline["round_trips"] == 3


def test_choose_keeps_the_calmer_train_row_and_ignores_a_later_name():
    rows = [
        {"id": "fast", "round_trips": 8, "min_equity": 9, "final_equity": 12, "capital": 10, "max_drawdown": 0.2},
        {"id": "calm", "round_trips": 6, "min_equity": 9.5, "final_equity": 10.2, "capital": 10, "max_drawdown": 0.04},
        {"id": "dead", "round_trips": 9, "min_equity": 0.1, "final_equity": 15, "capital": 10, "max_drawdown": 0.01},
    ]
    chosen, tag = choose_hf(rows)
    assert tag == "train_preserved"
    assert chosen["id"] == "calm"
    losing = [
        {"id": "worse", "round_trips": 6, "min_equity": 4, "final_equity": 6, "capital": 10, "max_drawdown": 0.4},
        {"id": "less", "round_trips": 7, "min_equity": 5, "final_equity": 8, "capital": 10, "max_drawdown": 0.3},
    ]
    chosen_loss, tag_loss = choose_hf(losing)
    assert tag_loss == "train_lost"
    assert chosen_loss["id"] == "less"
