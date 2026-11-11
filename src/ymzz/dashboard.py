"""多端儀表臺。預設只聽 127.0.0.1。不顯示、不記錄密鑰。"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ymzz.errors import YmzzError
from ymzz.forecast import describe_forecast
from ymzz.http_cache import DataClient
from ymzz.kraken_private import snapshot_private
from ymzz.redact import redact
from ymzz.watch import add_watch_address, fetch_balances, load_watchlist


def project_root() -> Path:
    env = os.environ.get("YMZZ_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / "dashboard.html").exists():
            return parent
    return Path.cwd()


class BacktestIn(BaseModel):
    start: str = "2026-01-01"
    end: str | None = None
    capital: float = 10
    size: float = 1
    sl: float = 0.05
    tp: float = 0.10
    fee_bp: float = 40
    slip_bp: float = 5
    daily_loss: float = 0.15
    rsi: float = 14
    rsi_low: float = 40
    rsi_high: float = 70
    rsi_exit: float = 75
    macd: str = "12,26,9"


class WatchIn(BaseModel):
    address: str = Field(min_length=1)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_log(path: Path, limit: int = 40) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-limit:]


def paper_view(root: Path) -> dict:
    state_path = root / "data" / "paper" / "state.json"
    state = _read_json(state_path)
    log_path = state_path.with_suffix("").with_name(state_path.stem + "_log.csv")
    if not log_path.exists():
        log_path = root / "data" / "paper" / "state_log.csv"
    equity = state.get("equity")
    initial = state.get("initial_capital")
    qty = float(state.get("qty") or 0)
    entry = float(state.get("entry_fill") or 0)
    mark = None
    curve = state.get("equity_curve") or []
    if curve:
        mark = curve[-1].get("price")
    unrealized = qty * (float(mark) - entry) if mark is not None and entry else 0.0
    realized = None
    if equity is not None and initial is not None:
        realized = float(equity) - float(initial) - unrealized
    sod = state.get("sod_equity")
    used = None
    if sod and equity is not None and float(sod) > 0:
        used = (float(sod) - float(equity)) / float(sod)
    sl = state.get("stop_price")
    tp = None
    if entry and state.get("tp_pct") is not None and qty > 0:
        tp = entry * (1.0 + float(state["tp_pct"]))
    return {
        "ok": True,
        "configured": bool(state),
        "state_path": str(state_path),
        "equity": equity,
        "drawdown": state.get("drawdown"),
        "daily_loss_used": used,
        "daily_loss_limit": state.get("daily_loss_halt_pct"),
        "halted": bool(state.get("halted")),
        "equity_curve": curve[-400:],
        "decisions": list(reversed(_read_log(log_path))),
        "sim_fills": state.get("sim_fills") or [],
        "position": {
            "qty": qty,
            "entry": entry or None,
            "stop_loss": sl,
            "take_profit": tp,
            "mark": mark,
            "unrealized_pnl": unrealized,
            "realized_pnl": realized,
        },
        "last_decision": state.get("last_decision"),
    }


def live_view(root: Path, reader=None) -> dict:
    local = _read_json(root / "data" / "live" / "state.json")
    curve_path = root / "data" / "live" / "equity.jsonl"
    curve = []
    if curve_path.exists():
        for line in curve_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                curve.append(json.loads(line))
    remote = reader() if reader else snapshot_private()
    remote = json.loads(redact(json.dumps(remote, ensure_ascii=False, default=str)))
    if local.get("stop_price") and remote.get("position"):
        remote["position"]["strategy_stop_loss"] = local.get("stop_price")
        entry = float(local.get("entry_fill") or 0)
        tp_pct = local.get("tp_pct")
        if entry and tp_pct is not None:
            remote["position"]["strategy_take_profit"] = entry * (1.0 + float(tp_pct))
        remote["position"]["strategy_entry"] = local.get("entry_fill")
    remote["equity_curve"] = curve[-400:]
    remote["local_state"] = bool(local)
    return remote


def status_view(root: Path) -> dict:
    beat = _read_json(root / "data" / "runner" / "heartbeat.json")
    if beat.get("last_error"):
        beat["last_error"] = redact(str(beat["last_error"]))
    return {
        "ok": True,
        "heartbeat": beat or None,
        "message_zh": "尚無心跳。runner 還沒啟動。" if not beat else "runner 心跳來自本機檔案。",
    }


def create_dashboard_app(root: Path | None = None, *, live_reader=None, balance_reader=None, backtest_runner=None, closes_loader=None) -> FastAPI:
    root = root or project_root()
    app = FastAPI(title="晏銘湛箴多端儀表臺", version="0.1.0")
    web = project_root() / "web"
    if not (web / "dashboard.html").exists():
        web = root / "web"

    @app.exception_handler(YmzzError)
    async def on_error(_request, exc: YmzzError):
        return JSONResponse(status_code=400, content={"ok": False, "message_zh": redact(exc.message_zh)})

    if web.exists():
        app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    def index():
        return FileResponse(web / "dashboard.html")

    @app.get("/api/dashboard/live")
    def live():
        return live_view(root, live_reader)

    @app.get("/api/dashboard/paper")
    def paper():
        return paper_view(root)

    @app.get("/api/dashboard/forecast")
    def forecast():
        paper = paper_view(root)
        params = {
            "rsi_period": 14,
            "rsi_low": 40,
            "rsi_high": 70,
            "rsi_exit": 75,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
        }
        if closes_loader is not None:
            closes = closes_loader()
        else:
            saved = root / "data" / "runner" / "forecast.json"
            if saved.exists():
                payload = json.loads(saved.read_text(encoding="utf-8"))
                return {"ok": True, "forecast": payload.get("forecast"), "params": payload.get("params") or params, "cached": True}
            try:
                from ymzz.data import load_market

                client = DataClient(root / "data" / "cache")
                bars, _funding, _prov = load_market(
                    {
                        "market": "crypto_spot",
                        "source": "kraken",
                        "symbol": "BTC/USD",
                        "timeframe": "1d",
                        "start": None,
                        "end": None,
                        "data_mode": "live",
                        "strict_range": False,
                    },
                    client,
                )
                closes = [bar.close for bar in bars]
            except YmzzError as exc:
                return {"ok": True, "message_zh": redact(exc.message_zh), "forecast": None, "params": params}
        report = describe_forecast(closes, params, qty=float((paper.get("position") or {}).get("qty") or 0), entry=(paper.get("position") or {}).get("entry"))
        return {"ok": True, "forecast": report, "params": params, "cached": False}

    @app.post("/api/dashboard/backtest")
    def backtest(body: BacktestIn):
        if backtest_runner is not None:
            return backtest_runner(body)
        from types import SimpleNamespace
        import contextlib
        import io

        from ymzz.cli import run_backtest_cli

        args = SimpleNamespace(
            strategy="rsi_macd",
            source="kraken",
            symbol="BTC/USD",
            timeframe="1d",
            start=body.start,
            end=body.end,
            rsi=body.rsi,
            rsi_low=body.rsi_low,
            rsi_high=body.rsi_high,
            rsi_exit=body.rsi_exit,
            macd=body.macd,
            sl=body.sl,
            tp=body.tp,
            size=body.size,
            capital=body.capital,
            fee_bp=body.fee_bp,
            slip_bp=body.slip_bp,
            daily_loss=body.daily_loss,
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            summary = run_backtest_cli(args, root)
        metrics = summary.get("metrics") or {}
        return {
            "ok": True,
            "metrics": metrics,
            "buy_hold_return_pct": summary.get("buy_hold_return_pct"),
            "bars": summary.get("bars"),
            "warnings": summary.get("warnings"),
            "report": summary.get("report"),
            "note_zh": "五筆或很少的成交不能證明優勢。這次回測沒有下單。",
        }

    @app.get("/api/dashboard/solana")
    def solana():
        addresses = load_watchlist(root)
        rows = []
        errors = []
        for address in addresses:
            try:
                if balance_reader is not None:
                    rows.append(balance_reader(address))
                else:
                    rows.append(fetch_balances(address, DataClient(root / "data" / "cache", min_interval=0.4)))
            except YmzzError as exc:
                errors.append({"address": address, "message_zh": redact(exc.message_zh)})
        return {
            "ok": True,
            "wallets": rows,
            "errors": errors,
            "read_only": True,
            "note_zh": "觀察名單只讀公開餘額。可以繼續加入地址。不會連接錢包，也不會判斷聰明錢。",
        }

    @app.post("/api/dashboard/wallets")
    def add_wallet(body: WatchIn):
        addresses = add_watch_address(root, body.address)
        return {"ok": True, "addresses": addresses}

    @app.get("/api/dashboard/status")
    def status():
        return status_view(root)

    @app.get("/api/health")
    def health():
        return {"ok": True, "name": "多端儀表臺", "bind_default": "127.0.0.1:8787"}

    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("YMZZ_DASHBOARD_HOST") or "127.0.0.1"
    port = int(os.environ.get("YMZZ_DASHBOARD_PORT") or "8787")
    uvicorn.run(create_dashboard_app(), host=host, port=port)
