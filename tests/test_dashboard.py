"""儀表臺、純 Python 紙上成交、runner 鎖。不打真實私有 API，不印密鑰。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ymzz.dashboard import create_dashboard_app
from ymzz.engine import Bar
from ymzz.errors import ConfigError, WalletError
from ymzz.forecast import describe_forecast
from ymzz.kraken_private import sign, snapshot_private
from ymzz.paper import step_decision
from ymzz.runner import InstanceLock, run_cycles
from ymzz.sim import build_runner, simulate_fill
from ymzz.watch import fetch_balances, validate_public_address

UTC = timezone.utc
SECRET = "super-secret-value-should-not-leak"
ADDRESS = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


def _bars(n, price=70_000.0):
    start = datetime(2026, 3, 1, tzinfo=UTC)
    return [
        Bar(start + timedelta(days=i), start + timedelta(days=i + 1), price, price, price, price, closed=True)
        for i in range(n)
    ]


def test_python_fill_applies_fee_and_slippage():
    fill = simulate_fill(side="buy", qty=1, ticker=100, fee_bp=40, slip_bp=5)
    assert fill["fill_price"] == pytest.approx(100.05)
    assert fill["fee"] == pytest.approx(0.4002)
    sell = simulate_fill(side="sell", qty=2, ticker=100, fee_bp=40, slip_bp=5)
    assert sell["fill_price"] == pytest.approx(99.95)


def test_missing_kraken_binary_simulates_paper_not_live(tmp_path):
    runner = build_runner(
        fee_bp=40,
        slip_bp=5,
        ledger_path=tmp_path / "sim.jsonl",
        ticker_fn=lambda: 50_000.0,
        which=lambda _name: None,
    )
    bought = runner(["kraken", "paper", "buy", "BTCUSD", "0.0001", "--type", "market", "--reason", "test"])
    assert bought.returncode == 0
    assert "ymzz-python" in bought.stdout
    assert SECRET not in bought.stdout
    refused = runner(["kraken", "order", "buy", "BTCUSD", "0.0001"])
    assert refused.returncode == 127
    assert "不會改用純 Python" in refused.stderr


def test_python_fill_changes_cash_once(tmp_path):
    def runner(argv):
        if argv[2] == "status":
            return SimpleNamespace(returncode=0, stdout='{"engine":"ymzz-python","fills":[]}', stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout='{"engine":"ymzz-python","side":"buy","qty":0.0001,"ticker":70000,"fill_price":70035,"fee":0.28,"fee_bp":40,"slip_bp":5}',
            stderr="",
        )

    common = dict(
        bars=_bars(2, 70_000),
        signals=[0.0, 1.0],
        indicator_exits=[0.0, 0.0],
        indicators={"rsi": [40.0, 55.0], "dif": [1.0, 2.0], "dea": [1.2, 1.0]},
        state_path=tmp_path / "state.json",
        csv_path=tmp_path / "log.csv",
        md_path=tmp_path / "log.md",
        capital=10,
        position_frac=1,
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
        runner=runner,
    )
    first = step_decision(**common)
    assert first["action"] == "buy"
    assert first["price"] == pytest.approx(70035)
    second = step_decision(**common)
    assert second["idempotent"] is True
    state = __import__("json").loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["cash"] == pytest.approx(10 - state["qty"] * 70035 - 0.28)
    assert len(state["equity_curve"]) == 1
    assert SECRET not in (tmp_path / "log.md").read_text(encoding="utf-8")


def test_runner_lock_and_idempotent_cycles(tmp_path):
    import threading

    lock = tmp_path / "ymzz.lock"
    beat = tmp_path / "heartbeat.json"
    held = threading.Event()
    release = threading.Event()

    def blocking(args):
        held.set()
        assert release.wait(2)
        return {"action": "hold", "idempotent": False, "bar_open": "a", "reason": "wait"}

    args = SimpleNamespace(mode="paper", interval=180)
    box = {}

    def target():
        box["beat"] = run_cycles(args=args, step=blocking, sleep=lambda _n: None, lock_path=lock, heartbeat_path=beat, cycles=1)

    thread = threading.Thread(target=target)
    thread.start()
    assert held.wait(2)
    with pytest.raises(ConfigError):
        run_cycles(args=args, step=lambda _a: {"action": "hold"}, sleep=lambda _n: None, lock_path=lock, heartbeat_path=beat, cycles=1)
    release.set()
    thread.join(3)
    assert "下一" not in (box["beat"].get("last_error") or "")
    assert box["beat"]["next_candle_check_utc"]

    calls = []

    def once(args):
        calls.append(args.mode)
        decision = step_decision(
            bars=_bars(2, 70_000),
            signals=[0.0, 1.0],
            indicator_exits=[0.0, 0.0],
            indicators={"rsi": [40.0, 50.0], "dif": [0.0, 2.0], "dea": [1.0, 1.0]},
            state_path=tmp_path / "state.json",
            csv_path=tmp_path / "log.csv",
            md_path=tmp_path / "log.md",
            capital=10,
            position_frac=1,
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
            runner=lambda argv: calls.append(argv) or SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        )
        return decision

    with InstanceLock(lock):
        pass
    run_cycles(args=args, step=once, sleep=lambda _n: None, lock_path=lock, heartbeat_path=beat, cycles=2)
    buys = [argv for argv in calls if isinstance(argv, list) and argv[:3] == ["kraken", "paper", "buy"]]
    assert len(buys) == 1
    saved = __import__("json").loads(beat.read_text(encoding="utf-8"))
    assert saved["idempotent"] is True
    assert SECRET not in beat.read_text(encoding="utf-8")


def test_sign_is_stable_and_snapshot_hides_secret(monkeypatch):
    secret = base64_secret()
    monkeypatch.setenv("KRAKEN_API_KEY", "public-looking-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", secret)
    first = sign("/0/private/Balance", {"nonce": "1"}, secret)
    second = sign("/0/private/Balance", {"nonce": "1"}, secret)
    assert first == second
    assert secret not in first

    def boom(_path, _data, _headers):
        raise RuntimeError(f"rejected {secret}")

    failed = snapshot_private(poster=boom)
    blob = __import__("json").dumps(failed)
    assert secret not in blob
    assert "[redacted]" in blob
    assert failed["configured"] is True
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    empty = snapshot_private(poster=boom)
    assert empty["status"] == "not configured"
    assert empty["ok"] is True


def base64_secret() -> str:
    import base64

    return base64.b64encode(b"0123456789abcdef").decode()


def test_dashboard_endpoints_do_not_leak_or_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_SECRET", SECRET)
    paper = tmp_path / "data" / "paper"
    paper.mkdir(parents=True)
    (paper / "state.json").write_text(
        '{"cash":6,"qty":0.00008,"entry_fill":70000,"stop_price":66500,"tp_pct":0.1,"sl_pct":0.05,'
        '"initial_capital":10,"equity":9.6,"drawdown":0.04,"sod_equity":10,"daily_loss_halt_pct":0.15,'
        '"halted":false,"equity_curve":[{"time_kst":"t","equity":10,"drawdown":0,"price":70000},'
        '{"time_kst":"t2","equity":9.6,"drawdown":0.04,"price":68000}],'
        '"last_decision":{"action":"hold","reason":"等待","rsi":"55","dif":"1","dea":"0.5"}}',
        encoding="utf-8",
    )
    (paper / "state_log.csv").write_text(
        "time_kst,bar_open,rsi,dif,dea,action,reason,qty,price\n"
        "t,b,55,1,0.5,hold,等待,0.00008,68000\n",
        encoding="utf-8",
    )
    runner = tmp_path / "data" / "runner"
    runner.mkdir(parents=True)
    (runner / "heartbeat.json").write_text(
        '{"mode":"paper","beat_kst":"2026-11-11T13:00:00+09:00","next_heartbeat_utc":"x","next_candle_check_utc":"y","last_error":null,"last_action":"hold"}',
        encoding="utf-8",
    )

    def live_reader():
        return {
            "configured": True,
            "ok": True,
            "status": "ok",
            "message_zh": f"餘額正常 {SECRET}",
            "balances": [{"asset": "ZUSD", "amount": "10"}],
            "open_orders": [],
            "trades": [],
            "position": {"asset": "XBT", "qty": 0.0, "stop_loss": None, "take_profit": None, "last": None},
            "equity": 10,
            "unrealized_pnl": 0,
            "realized_pnl": 0,
            "realized_note_zh": "測試",
        }

    closes = [100 + i for i in range(80)]
    app = create_dashboard_app(
        tmp_path,
        live_reader=live_reader,
        balance_reader=lambda address: {"address": address, "sol": 1.5, "tokens": [], "read_only": True},
        closes_loader=lambda: closes,
        backtest_runner=lambda body: {"ok": True, "metrics": {"final_equity": body.capital, "round_trips": 0}, "warnings": []},
    )
    client = TestClient(app)
    live = client.get("/api/dashboard/live")
    assert live.status_code == 200
    assert SECRET not in live.text
    paper_body = client.get("/api/dashboard/paper").json()
    assert paper_body["position"]["stop_loss"] == 66500
    assert paper_body["decisions"][0]["rsi"] == "55"
    forecast = client.get("/api/dashboard/forecast").json()
    assert forecast["forecast"]["rsi14"] is not None
    status = client.get("/api/dashboard/status").json()
    assert status["heartbeat"]["mode"] == "paper"
    added = client.post("/api/dashboard/wallets", json={"address": ADDRESS})
    assert added.status_code == 200
    listed = client.get("/api/dashboard/solana").json()
    assert listed["wallets"][0]["sol"] == 1.5
    assert listed["read_only"] is True
    rejected = client.post("/api/dashboard/wallets", json={"address": "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"})
    assert rejected.status_code == 400
    page = client.get("/")
    assert "多端儀表臺" in page.text
    ran = client.post("/api/dashboard/backtest", json={"capital": 10, "start": "2026-01-01"})
    assert ran.json()["metrics"]["round_trips"] == 0


def test_sweep_view_serves_summary_then_one_curve(tmp_path):
    payload = {
        "recommendation": {"id": "BTC/USD|1d|macd", "reason_zh": "回撤較小"},
        "coverage": [],
        "rows": [
            {
                "id": "BTC/USD|1d|macd",
                "symbol": "BTC/USD",
                "timeframe": "1d",
                "strategy": "macd",
                "chase": False,
                "total_return": 0.02,
                "max_drawdown": 0.04,
                "return_over_dd": 0.5,
                "round_trips": 6,
                "final_equity": 10.2,
                "equity": [{"time": "2026-01-01", "equity": 10, "drawdown": 0}],
                "trades": [{"time": "2026-01-02", "action": "buy", "price": 100, "reason": "signal", "fee": 0.01}],
            }
        ],
    }
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "crypto_sweep.json").write_text(json.dumps(payload), encoding="utf-8")
    client = TestClient(create_dashboard_app(tmp_path))
    summary = client.get("/api/dashboard/sweep").json()
    assert summary["ok"] is True
    assert "equity" not in summary["rows"][0]
    detail = client.get("/api/dashboard/sweep", params={"id": "BTC/USD|1d|macd"}).json()
    assert detail["row"]["trades"][0]["action"] == "buy"
    page = client.get("/").text
    assert "策略掃描" in page
    empty = TestClient(create_dashboard_app(tmp_path / "missing"))
    assert empty.get("/api/dashboard/sweep").json()["rows"] == []


def test_public_address_and_balance_parser():
    assert validate_public_address(ADDRESS) == ADDRESS
    with pytest.raises(WalletError):
        validate_public_address("not an address")

    class Client:
        def post_json(self, url, body, mode="live", headers=None):
            assert "mainnet-beta.solana.com" in url
            if body["method"] == "getBalance":
                return {"result": {"value": 2_000_000_000}}, {"sha256": "a"}
            return {"result": {"value": []}}, {"sha256": "b"}

    report = fetch_balances(ADDRESS, Client())
    assert report["sol"] == pytest.approx(2)
    assert report["read_only"] is True


def test_forecast_distance_uses_closed_bars():
    closes = [100 + i * 0.2 for i in range(80)]
    report = describe_forecast(closes, {"rsi_period": 14, "rsi_low": 40, "rsi_high": 70, "rsi_exit": 75, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9})
    assert report["rsi14"] is not None
    assert report["distance_dif_minus_dea"] is not None
    assert "下一根" in report["next_candle_zh"]
