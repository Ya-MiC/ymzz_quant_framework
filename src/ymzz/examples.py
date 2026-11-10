"""真實資料範例。參數在看到績效之前就寫死，不為獲利改週期。"""

from __future__ import annotations

import csv
import subprocess
import sys
import traceback
from pathlib import Path

from ymzz.data import StockAppAdapter, load_market
from ymzz.engine import CostConfig, EngineConfig, run_backtest
from ymzz.errors import DataGapError, YmzzError
from ymzz.http_cache import DataClient
from ymzz.snapshot import write_snapshot
from ymzz.strategies import compute_signals
from ymzz.timeutil import now_utc, to_kst
from ymzz.wallet import inspect_address

ROOT = Path(__file__).resolve().parents[2]
START = "2026-01-01"
END = "2026-11-10"


def _client() -> DataClient:
    return DataClient(ROOT / "data" / "cache", min_interval=0.3)


def _run(spec: dict, strategy: str, params: dict, engine: EngineConfig, note: str) -> dict:
    client = _client()
    attempted = dict(spec)
    gap_note = ""
    try:
        bars, funding, prov = load_market(attempted, client)
    except DataGapError as exc:
        earliest = exc.extra.get("earliest")
        gap_note = exc.message_zh
        if not earliest:
            raise
        attempted = {**attempted, "start": earliest}
        bars, funding, prov = load_market(attempted, client)
        gap_note += f" 已用同一來源的實際起點 {earliest} 再請求一次，沒有更換來源。"
    signals, indicators, warnings = compute_signals(strategy, [bar.close for bar in bars], params, engine.allow_short)
    result = run_backtest(bars, signals, funding, engine)
    return {
        "note": note,
        "gap_note": gap_note,
        "spec": attempted,
        "strategy": strategy,
        "params": params,
        "bars": bars,
        "indicators": indicators,
        "signals": signals,
        "provenance": prov,
        "result": result,
        "warnings": warnings + result.warnings,
    }


def _probe(name: str, fn) -> dict:
    try:
        payload = fn()
        return {"name": name, "ok": True, **payload}
    except Exception as exc:  # noqa: BLE001 — 探測要留下來源原文
        message = exc.message_zh if isinstance(exc, YmzzError) else f"{exc.__class__.__name__}: {exc}"
        return {"name": name, "ok": False, "error": message}


def _fmt(value, digits=4):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value):
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _write_ledger(path: Path, run: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["時間", "動作", "價格", "數量", "費用", "滑點bp", "原因", "錢包", "部位", "權益"])
        for row in run["result"].trades:
            writer.writerow(
                [
                    row["time"],
                    row["action"],
                    row["price"],
                    row["qty"],
                    row["fee"],
                    row["slippage_bps"],
                    row["reason"],
                    row["wallet_after"],
                    row["position_after"],
                    row["equity_after"],
                ]
            )


def _section(run: dict) -> str:
    metrics = run["result"].metrics
    prov = run["provenance"]
    last = run["bars"][-1]
    lines = [
        f"### {run['note']}",
        "",
        f"- 請求：`{run['spec']['market']}` / `{run['spec'].get('source')}` / `{run['spec'].get('symbol')}` / `{run['spec'].get('timeframe')}`",
        f"- 策略：`{run['strategy']}` 參數 {run['params']}",
        f"- 本金 {metrics['initial_capital']}，槓桿 {metrics['leverage']}，工具 {metrics['instrument']}",
        f"- K 線 {len(run['bars'])} 根，最後收盤 {last.close}（{last.open_time.isoformat()}）",
        f"- 期末權益 {_fmt(metrics['final_equity'])}，報酬 {_pct(metrics['total_return'])}，最大回撤 {_pct(metrics['max_drawdown'])}",
        f"- 手續費 {_fmt(metrics['fees_paid'])}，資金費 {_fmt(metrics['funding_paid'])}，清算 {metrics['liquidations']}，成交 {metrics['trade_count']}",
        f"- 未平倉數量 {_fmt(metrics['open_qty'])}。未平倉只是最後收盤的市值，不是額外成交。",
        f"- 來源 {prov.get('source_url')} ，抓取 {prov.get('fetched_at_kst')} ，sha256 `{str(prov.get('sha256', ''))[:16]}`",
        f"- 剔除未收盤 {prov.get('dropped_unclosed')}。{prov.get('note_zh', '')}",
    ]
    if run["gap_note"]:
        lines.append(f"- 區間：{run['gap_note']}")
    if prov.get("funding_points"):
        lines.append(f"- 資金費事件 {prov.get('funding_points')} 筆。")
    if run["warnings"]:
        lines.append("- 警告：" + " ".join(run["warnings"][:6]))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    kst = to_kst(now_utc())
    stamp = kst.strftime("%m%d_%H%M")
    results = ROOT / "results"
    results.mkdir(parents=True, exist_ok=True)
    runs = []
    failures = []

    jobs = [
        (
            "btc_spot",
            "BTC 現貨日線，RSI 三線 6/12/24，1 倍，本金 10 USDT",
            {
                "market": "crypto_spot",
                "source": "okx",
                "symbol": "BTC/USDT",
                "timeframe": "1d",
                "start": START,
                "end": END,
            },
            "rsi_three",
            {"rsi_short": 6, "rsi_mid": 12, "rsi_long": 24},
            EngineConfig(10, 1, "spot", CostConfig(10, 5), tp_pct=0.10, sl_pct=0.05),
        ),
        (
            "ashare",
            "600519 日線，均線 5/20，1 倍，本金 20 萬元",
            {
                "market": "ashare",
                "source": "sina",
                "symbol": "600519",
                "timeframe": "1d",
                "start": START,
                "end": END,
            },
            "ma_cross",
            {"fast": 5, "slow": 20},
            EngineConfig(
                200_000,
                1,
                "spot",
                CostConfig(2.5, 5, stamp_tax_bps=5, min_commission=5),
                tp_pct=0.10,
                sl_pct=0.05,
                lot_size=100,
                intrabar_exit_on_entry=False,
            ),
        ),
        (
            "btc_perp",
            "BTC 永續日線，MACD 12/26/9，2 倍，本金 10 USDT",
            {
                "market": "crypto_perp",
                "source": "okx",
                "symbol": "BTC/USDT",
                "timeframe": "1d",
                "start": START,
                "end": END,
            },
            "macd",
            {"fast": 12, "slow": 26, "signal": 9},
            EngineConfig(10, 2, "perpetual", CostConfig(4, 5), tp_pct=0.10, sl_pct=0.05, mmr=0.004),
        ),
        (
            "polymarket",
            "Polymarket 559651 的 Yes 價格點，RSI 14，1 倍，本金 10 USDC，手續費參數 0",
            {
                "market": "polymarket",
                "source": "polymarket",
                "symbol": "559651",
                "timeframe": "1d",
                "start": START,
                "end": END,
            },
            "rsi_single",
            {"period": 14, "oversold": 30, "overbought": 70},
            EngineConfig(10, 1, "spot", CostConfig(0, 5), tp_pct=0.10, sl_pct=0.05),
        ),
    ]
    for key, note, spec, strategy, params, engine in jobs:
        try:
            print("run", key, flush=True)
            run = _run(spec, strategy, params, engine, note)
            runs.append((key, run))
            _write_ledger(results / f"{stamp}_seoul_{key}_ledger.csv", run)
        except Exception as exc:  # noqa: BLE001
            failures.append({"name": key, "error": exc.message_zh if isinstance(exc, YmzzError) else str(exc), "trace": traceback.format_exc()})
            print("fail", key, exc, flush=True)

    def binance():
        load_market(
            {"market": "crypto_spot", "source": "binance", "symbol": "BTC/USDT", "timeframe": "1d", "start": START, "end": END},
            _client(),
        )
        return {"detail": "不該成功卻成功了"}

    def kraken():
        bars, _funding, prov = load_market(
            {"market": "crypto_spot", "source": "kraken", "symbol": "BTC/USD", "timeframe": "1d", "start": "2026-10-01", "end": END},
            _client(),
        )
        return {"bars": len(bars), "last": bars[-1].close, "sha": prov.get("sha256", "")[:12]}

    def eastmoney():
        bars, _f, prov = load_market(
            {
                "market": "ashare",
                "source": "eastmoney",
                "symbol": "600519",
                "timeframe": "1d",
                "start": "2026-10-01",
                "end": END,
                "source_host": "80.push2his.eastmoney.com",
            },
            _client(),
        )
        return {"bars": len(bars), "last": bars[-1].close, "host": prov.get("host"), "quote": bars[-1].quote_volume}

    def tencent():
        bars, _f, prov = load_market(
            {"market": "ashare", "source": "tencent", "symbol": "600519", "timeframe": "1d", "start": "2026-10-01", "end": END},
            _client(),
        )
        return {"bars": len(bars), "last": bars[-1].close, "adjust": prov.get("adjust")}

    def gecko():
        bars, _f, prov = load_market(
            {
                "market": "solana_pool",
                "source": "geckoterminal",
                "symbol": "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",
                "timeframe": "1d",
                "start": "2026-09-01",
                "end": END,
            },
            _client(),
        )
        return {"bars": len(bars), "last": bars[-1].close, "dropped": prov.get("dropped_unclosed")}

    def polymarket():
        bars, _f, prov = load_market(
            {"market": "polymarket", "source": "polymarket", "symbol": "559651", "timeframe": "1d", "start": "2026-08-01", "end": END},
            _client(),
        )
        return {"bars": len(bars), "last": bars[-1].close, "question": prov.get("question"), "outcome": prov.get("outcome")}

    def stock_app():
        StockAppAdapter().load()
        return {}

    print("probes", flush=True)
    probes = [
        _probe("binance_spot", binance),
        _probe("kraken_spot", kraken),
        _probe("eastmoney_80", eastmoney),
        _probe("tencent_qfq", tencent),
        _probe("geckoterminal_sol_usdc", gecko),
        _probe("polymarket_559651", polymarket),
        _probe("stock_app", stock_app),
    ]
    wallet = _probe(
        "solana_fee_payer",
        lambda: inspect_address("EMCcpe4aoH2rgzUTJJVk8fnbcGBiMjeh3Y8Ynfdw2WrJ", _client(), limit=3),
    )
    pool = _probe(
        "solana_pool_inventory",
        lambda: inspect_address("Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE", _client(), limit=1),
    )

    pytest_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": "src"},
    )
    pytest_tail = "\n".join((pytest_run.stdout + pytest_run.stderr).strip().splitlines()[-8:])

    if runs:
        key, sample = runs[0]
        snap = {
            "warnings": sample["warnings"],
            "metrics": sample["result"].metrics,
            "trades": sample["result"].trades,
            "equity": sample["result"].equity,
            "indicators": sample["indicators"],
            "bars": [
                {
                    "time": bar.open_time.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in sample["bars"]
            ],
            "provenance": sample["provenance"],
        }
        write_snapshot(results / f"{stamp}_seoul_{key}_snapshot.html", snap, ROOT)

    report = [f"# 晏銘湛箴真實資料回測紀錄", "", f"- 撰寫時間：{kst.isoformat()}（Asia/Seoul）", f"- 檔名戳記：{stamp}", ""]
    report.append("參數在程式裡先寫好才跑：RSI 6/12/24、均線 5/20、MACD 12/26/9、止盈 10%、止損 5%、滑點 5bp。沒有在看到報酬之後改這些數字。")
    report.append("")
    report.append("## 回測")
    report.append("")
    if not runs:
        report.append("三筆指定回測都沒有跑完，見下方錯誤。")
    for _key, run in runs:
        report.append(_section(run))
    if failures:
        report.append("## 回測錯誤")
        report.append("")
        for item in failures:
            report.append(f"- {item['name']}：{item['error']}")
        report.append("")
    report.append("## 來源探測")
    report.append("")
    report.append("| 探測 | 結果 | 說明 |")
    report.append("| --- | --- | --- |")
    for item in probes + [wallet, pool]:
        if item["ok"]:
            detail = {k: v for k, v in item.items() if k not in {"name", "ok", "events", "caveats_zh", "provenance", "errors"}}
            if "counts" in item:
                detail = {"counts": item["counts"], "errors": len(item.get("errors") or [])}
            report.append(f"| {item['name']} | 成功 | `{detail}` |")
        else:
            report.append(f"| {item['name']} | 失敗 | {item['error'][:220]} |")
    report.append("")
    report.append("幣安若是 451，代表這個環境被地理限制擋住。適配器沒有改去 OKX。OKX 的回測是另行指定的來源。")
    report.append("Polymarket 若成功，歷史是價格點，不是 K 線高低，也不是事件結算賠付。")
    report.append("東財主機這次用 `80.push2his.eastmoney.com`。`92.push2his` 與裸的 `push2his` 在寫這份程式時對本環境是空回覆，所以沒有把它們寫成自動備援。")
    report.append("Solana 那個手續費地址只是先前一筆公開交易裡的付款人，不是聰明錢名單。池地址的庫存變化不是個人跟單績效。這次沒有把池子拿去算跟單報酬，避免把庫存變動說成某人的損益。跟單的延遲與滑點由單元測試鎖定。")
    report.append("")
    report.append("## 功能矩陣")
    report.append("")
    report.append("| 項目 | 狀態 |")
    report.append("| --- | --- |")
    rows = [
        ("六個策略與參數", "完成"),
        ("槓桿 1 / 1.5 / 2，後端拒絕更高；現貨拒絕大於 1", "完成"),
        ("永續保證金、清算、資金費、不追加", "完成；範例走 OKX"),
        ("手續費、滑點、下一根開盤、同根先止損", "完成"),
        ("A 股日線", "完成（新浪回測；騰訊與東財 80 另有探測）"),
        ("BTC 現貨與永續", "完成（OKX）。幣安端點在，但是否通取決於網路"),
        ("Kraken 現貨", "有探測，沒有當成第二套績效"),
        ("GeckoTerminal 池 K 線", "見探測"),
        ("Polymarket Gamma + prices-history", "有一筆 RSI 回測；價格點不是 K 線。失敗就停，不編價格"),
        ("公開錢包分類與跟單紙上重播", "完成程式。這次個人地址若沒有兌換，就不編造跟單報酬"),
        ("Telegram aiogram 紙上訊號", "完成骨架，權杖只讀環境變數，這次沒有連線發訊"),
        ("GMGN 下單", "缺少，只留會丟出錯誤的介面"),
        ("Stock App 打金狗", "缺少，刻意不代換"),
        ("網頁與設定存取", "完成"),
        ("實盤", "缺少，刻意不做"),
    ]
    for name, status in rows:
        report.append(f"| {name} | {status} |")
    report.append("")
    report.append("## 測試")
    report.append("")
    report.append("```")
    report.append(pytest_tail)
    report.append("```")
    report.append("")
    report.append("## 沒有做的事")
    report.append("")
    report.append("沒有私鑰、沒有下單、沒有付費 API、沒有用 yfinance 冒充 Stock App、沒有把未收盤 K 線算進回測、沒有在來源失敗時悄悄換所。10 美元買不了貴州茅台一手，所以 A 股範例用 20 萬元，並在資金不足時拒絕零股。")
    text = "\n".join(report) + "\n"
    report_path = results / f"{stamp}_seoul_ymzz_backtest.md"
    report_path.write_text(text, encoding="utf-8")
    print(report_path, flush=True)
    for folder in (Path("/cursor/stores/self/deliverables"), Path("/opt/cursor/artifacts")):
        folder.mkdir(parents=True, exist_ok=True)
        for path in results.glob(f"{stamp}_seoul_*"):
            target = folder / path.name
            target.write_bytes(path.read_bytes())
    if pytest_run.returncode != 0:
        raise SystemExit(pytest_run.returncode)


if __name__ == "__main__":
    main()
