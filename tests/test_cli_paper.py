"""指令與紙上狀態。Kraken 指令用假的 runner，不送真單、不印密鑰。"""

from datetime import datetime, timedelta, timezone

import pytest

from ymzz.engine import Bar
from ymzz.errors import ConfigError
from ymzz.paper import LiveRefused, guard_live, order_commands, step_decision

UTC = timezone.utc
SECRET = "super-secret-value-should-not-leak"


def _bars(n, price=100.0):
    start = datetime(2026, 3, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        opened = start + timedelta(days=i)
        out.append(Bar(opened, opened + timedelta(days=1), price, price, price, price, closed=True))
    return out


def _runner(calls):
    def run(argv):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "{}"})()

    return run


def test_live_refuses_without_keys_or_confirm(monkeypatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    with pytest.raises(LiveRefused) as missing:
        guard_live(live=True, confirm=True, capital=10, leverage=1, instrument="spot")
    assert SECRET not in str(missing.value)
    monkeypatch.setenv("KRAKEN_API_KEY", SECRET)
    monkeypatch.setenv("KRAKEN_API_SECRET", SECRET)
    with pytest.raises(LiveRefused) as no_flag:
        guard_live(live=True, confirm=False, capital=10, leverage=1, instrument="spot")
    assert SECRET not in str(no_flag.value)
    with pytest.raises(LiveRefused):
        guard_live(live=True, confirm=True, capital=10.01, leverage=1, instrument="spot")
    with pytest.raises(LiveRefused):
        guard_live(live=True, confirm=True, capital=10, leverage=2, instrument="spot")
    guard_live(live=False, confirm=False, capital=10_000, leverage=2, instrument="perpetual")


def test_live_buy_always_attaches_stop_and_omits_secrets():
    commands = order_commands(live=True, side="buy", pair="BTCUSD", qty=0.0001, reason="signal", stop_price=95)
    blob = " ".join(" ".join(cmd) for cmd in commands)
    assert "stop-loss" in blob
    assert SECRET not in blob
    assert commands[0][:3] == ["kraken", "order", "buy"]
    paper = order_commands(live=False, side="buy", pair="BTCUSD", qty=0.0001, reason="signal", stop_price=95)
    assert paper[0][:3] == ["kraken", "paper", "buy"]
    assert paper[-1] == ["kraken", "paper", "status"]
    with pytest.raises(LiveRefused):
        order_commands(live=True, side="buy", pair="BTCUSD", qty=0.0001, reason="signal", stop_price=None)


def test_paper_step_is_idempotent_and_logs_below_minimum(tmp_path, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", SECRET)
    bars = _bars(3, price=100_000)
    signals = [0.0, 0.0, 1.0]
    exits = [0.0, 0.0, 0.0]
    indicators = {"rsi": [None, None, 55.0], "dif": [None, None, 1.0], "dea": [None, None, 0.5]}
    calls = []
    common = dict(
        bars=bars,
        signals=signals,
        indicator_exits=exits,
        indicators=indicators,
        state_path=tmp_path / "state.json",
        csv_path=tmp_path / "log.csv",
        md_path=tmp_path / "log.md",
        capital=10,
        position_frac=0.30,
        sl_pct=0.05,
        tp_pct=0.10,
        daily_loss_halt_pct=0.15,
        ordermin=0.00006,
        costmin=0.5,
        lot_decimals=8,
        symbol="BTC/USD",
        live=False,
        confirm=False,
        leverage=1,
        runner=_runner(calls),
    )
    first = step_decision(**common)
    assert first["action"] == "skip"
    assert first["reason"] == "below minimum"
    assert calls == []
    second = step_decision(**common)
    assert second["idempotent"] is True
    assert calls == []
    assert SECRET not in (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "below minimum" in (tmp_path / "log.csv").read_text(encoding="utf-8")


def test_paper_buy_once_then_repeat_does_not_order(tmp_path):
    bars = _bars(2, price=70_000)
    calls = []
    common = dict(
        bars=bars,
        signals=[0.0, 1.0],
        indicator_exits=[0.0, 0.0],
        indicators={"rsi": [40.0, 50.0], "dif": [0.0, 2.0], "dea": [1.0, 1.0]},
        state_path=tmp_path / "state.json",
        csv_path=tmp_path / "log.csv",
        md_path=tmp_path / "log.md",
        capital=10,
        position_frac=1.0,
        sl_pct=0.05,
        tp_pct=0.10,
        daily_loss_halt_pct=0.15,
        ordermin=0.00006,
        costmin=0.5,
        lot_decimals=8,
        symbol="BTC/USD",
        live=False,
        confirm=False,
        leverage=1,
        runner=_runner(calls),
    )
    bought = step_decision(**common)
    assert bought["action"] == "buy"
    assert any(cmd[:3] == ["kraken", "paper", "buy"] for cmd in calls)
    assert any(cmd == ["kraken", "paper", "status"] for cmd in calls)
    n = len(calls)
    again = step_decision(**common)
    assert again["idempotent"] is True
    assert len(calls) == n


def test_stop_wins_when_same_bar_also_hits_target(tmp_path):
    opened = datetime(2026, 4, 1, tzinfo=UTC)
    bars = [Bar(opened, opened + timedelta(days=1), 100, 120, 90, 110, closed=True)]
    calls = []
    state = tmp_path / "state.json"
    state.write_text('{"qty": 0.001, "entry_fill": 100, "cash": 0}', encoding="utf-8")
    decision = step_decision(
        bars=bars,
        signals=[0.0],
        indicator_exits=[1.0],
        indicators={"rsi": [80.0], "dif": [1.0], "dea": [2.0]},
        state_path=state,
        csv_path=tmp_path / "log.csv",
        md_path=tmp_path / "log.md",
        capital=10,
        position_frac=1,
        sl_pct=0.05,
        tp_pct=0.10,
        daily_loss_halt_pct=None,
        ordermin=0.00006,
        costmin=0.5,
        lot_decimals=8,
        symbol="BTC/USD",
        live=False,
        confirm=False,
        leverage=1,
        runner=_runner(calls),
    )
    assert decision["action"] == "stop_loss"
    assert calls[0][2] == "sell"


def test_live_flag_does_not_call_runner_when_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", SECRET)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    calls = []
    with pytest.raises(ConfigError) as exc:
        step_decision(
            bars=_bars(1, 70_000),
            signals=[1.0],
            indicator_exits=[0.0],
            indicators={"rsi": [50.0], "dif": [1.0], "dea": [0.0]},
            state_path=tmp_path / "state.json",
            csv_path=tmp_path / "log.csv",
            md_path=tmp_path / "log.md",
            capital=10,
            position_frac=1,
            sl_pct=0.05,
            tp_pct=0.10,
            daily_loss_halt_pct=0.15,
            ordermin=0,
            costmin=0,
            lot_decimals=8,
            symbol="BTC/USD",
            live=True,
            confirm=True,
            leverage=1,
            runner=_runner(calls),
        )
    assert calls == []
    assert SECRET not in str(exc.value)
