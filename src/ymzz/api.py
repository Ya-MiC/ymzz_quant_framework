"""本地研究 API。不下單。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ymzz.catalog import CATALOG, get_market
from ymzz.data import load_kraken_limits, load_market
from ymzz.engine import CostConfig, EngineConfig, run_backtest
from ymzz.errors import ConfigError, LeverageError, StockAppUnidentified, YmzzError
from ymzz.http_cache import DataClient
from ymzz.strategies import compute_signals, list_strategies
from ymzz.wallet import inspect_address, replay_swaps


def project_root() -> Path:
    import os

    env = os.environ.get("YMZZ_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / "index.html").exists():
            return parent
    return Path.cwd()


class BacktestIn(BaseModel):
    market: str
    source: str | None = None
    symbol: str
    timeframe: str = "1d"
    start: str | None = None
    end: str | None = None
    strategy: str
    params: dict = Field(default_factory=dict)
    leverage: float = 1
    capital: float = 10
    fee_bps: float = 10
    slippage_bps: float = 5
    impact_coef: float = 0
    stamp_tax_bps: float = 0
    min_commission: float = 0
    tp_pct: float = 0.1
    sl_pct: float = 0.05
    mmr: float = 0.004
    allow_short: bool = False
    strict_range: bool = True
    source_host: str | None = None
    data_mode: str = "live"
    position_frac: float = 1.0
    daily_loss_halt_pct: float | None = None
    ordermin: float | None = None
    costmin: float | None = None
    lot_decimals: int | None = None


class ConfigIn(BaseModel):
    name: str
    config: dict


class WalletIn(BaseModel):
    address: str
    limit: int = 8


class CopyIn(BaseModel):
    events: list[dict]
    bars: list[dict]
    delay_seconds: float = 60
    slippage_bps: float = 30
    capital: float = 10
    base_mint: str


def _root() -> Path:
    return project_root()


def create_app() -> FastAPI:
    app = FastAPI(title="晏銘湛箴量化研究", version="0.1.0")
    web = _root() / "web"
    if web.exists():
        app.mount("/static", StaticFiles(directory=web), name="static")

    @app.exception_handler(YmzzError)
    async def on_error(_request, exc: YmzzError):
        status = 502 if exc.error_code in {"DATA_SOURCE", "DATA_GAP", "CACHE_MISS", "WALLET"} else 400
        return JSONResponse(
            status_code=status,
            content={"ok": False, "error_code": exc.error_code, "message_zh": exc.message_zh, **exc.extra},
        )

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "name": "晏銘湛箴", "live_trading": False}

    @app.get("/api/catalog")
    def catalog():
        return {"markets": CATALOG}

    @app.get("/api/strategies")
    def strategies():
        return {"strategies": list_strategies()}

    @app.post("/api/backtest")
    def backtest(body: BacktestIn):
        if body.market == "stock_app":
            raise StockAppUnidentified(
                "Stock App 打金狗的產品身分還沒有被指認。不會用 yfinance、A 股或 GMGN 頂替，也不會跑假回測。"
            )
        try:
            market = get_market(body.market)
        except KeyError as exc:
            raise ConfigError("沒有這個市場") from exc
        instrument = market["instrument"]
        if not any(abs(float(body.leverage) - float(item)) < 1e-9 for item in market["leverage"]):
            raise LeverageError("這個市場不能用這個槓桿。現貨只有 1 倍；永續最多 2 倍。")
        # 先擋槓桿，避免為了錯誤參數去打公開 API。
        EngineConfig(capital=body.capital, leverage=body.leverage, instrument=instrument).validated()
        client = DataClient(_root() / "data" / "cache")
        bars, funding, provenance = load_market(body.model_dump(), client)
        closes = [bar.close for bar in bars]
        signals, indicators, warnings = compute_signals(body.strategy, closes, body.params, body.allow_short and instrument == "perpetual")
        indicator_exits = indicators.pop("_indicator_exit", None)
        ordermin = body.ordermin
        costmin = body.costmin
        lot_decimals = body.lot_decimals
        if body.source == "kraken" and (ordermin is None or costmin is None or lot_decimals is None):
            limits, limit_prov = load_kraken_limits(client, body.data_mode)
            ordermin = limits["ordermin"] if ordermin is None else ordermin
            costmin = limits["costmin"] if costmin is None else costmin
            lot_decimals = limits["lot_decimals"] if lot_decimals is None else lot_decimals
            provenance = {**provenance, "kraken_limits": limits, "kraken_limits_sha256": limit_prov.get("sha256")}
            warnings.append(
                f"Kraken 最小下單量來自公開 AssetPairs：ordermin {ordermin}、costmin {costmin}、小數位 {lot_decimals}。"
            )
        elif ordermin is None and costmin is None:
            ordermin = 0.0
            costmin = 0.0
            warnings.append("這個來源沒有查詢最小下單量，本回測不套用最小量過濾。")
        halt = body.daily_loss_halt_pct if body.daily_loss_halt_pct and body.daily_loss_halt_pct > 0 else None
        result = run_backtest(
            bars,
            signals,
            funding,
            EngineConfig(
                capital=body.capital,
                leverage=body.leverage,
                instrument=instrument,
                cost=CostConfig(
                    fee_bps=body.fee_bps,
                    slippage_bps=body.slippage_bps,
                    impact_coef=body.impact_coef,
                    stamp_tax_bps=body.stamp_tax_bps,
                    min_commission=body.min_commission,
                ),
                tp_pct=body.tp_pct,
                sl_pct=body.sl_pct,
                mmr=body.mmr,
                lot_size=float(market.get("lot_size") or 0),
                intrabar_exit_on_entry=body.market != "ashare",
                allow_short=body.allow_short and instrument == "perpetual",
                position_frac=body.position_frac,
                daily_loss_halt_pct=halt,
                ordermin=float(ordermin or 0),
                costmin=float(costmin or 0),
                lot_decimals=lot_decimals,
                signal_mode="pulse" if indicator_exits is not None else "target",
            ),
            indicator_exits=indicator_exits,
        )
        return {
            "ok": True,
            "provenance": provenance,
            "warnings": warnings + result.warnings,
            "metrics": result.metrics,
            "trades": result.trades,
            "skips": result.skips,
            "round_trips": result.round_trips,
            "equity": result.equity,
            "indicators": indicators,
            "signals": signals,
            "bars": [
                {
                    "time": bar.open_time.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in bars
            ],
            "strategy": body.strategy,
            "live_trading": False,
        }

    @app.post("/api/config")
    def save_config(body: ConfigIn):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", body.name):
            raise ConfigError("設定名稱只能用英數、底線與減號")
        path = _root() / "configs" / f"{body.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body.config, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "name": body.name}

    @app.get("/api/config/{name}")
    def load_config(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name):
            raise ConfigError("設定名稱不合法")
        path = _root() / "configs" / f"{name}.json"
        if not path.exists():
            raise ConfigError("找不到這份設定")
        return {"ok": True, "name": name, "config": json.loads(path.read_text(encoding="utf-8"))}

    @app.get("/api/config")
    def list_config():
        folder = _root() / "configs"
        names = sorted(path.stem for path in folder.glob("*.json")) if folder.exists() else []
        return {"names": names}

    @app.post("/api/wallet/inspect")
    def wallet_inspect(body: WalletIn):
        client = DataClient(_root() / "data" / "cache", min_interval=0.4)
        report = inspect_address(body.address, client, body.limit)
        report["ok"] = True
        return report

    @app.post("/api/wallet/copy-backtest")
    def wallet_copy(body: CopyIn):
        from ymzz.engine import Bar
        from datetime import datetime

        bars = []
        for row in body.bars:
            opened = datetime.fromisoformat(row["time"])
            bars.append(
                Bar(
                    opened,
                    opened,
                    float(row["open"]),
                    float(row.get("high", row["open"])),
                    float(row.get("low", row["open"])),
                    float(row.get("close", row["open"])),
                    closed=True,
                )
            )
        result = replay_swaps(
            body.events,
            bars,
            delay_seconds=body.delay_seconds,
            slippage_bps=body.slippage_bps,
            capital=body.capital,
            base_mint=body.base_mint,
        )
        result["ok"] = True
        return result

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
