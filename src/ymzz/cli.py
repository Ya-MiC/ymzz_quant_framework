"""使用者自己跑的指令。不依賴助理，也不會在回測時下單。

回測：

    python -m ymzz.cli backtest --strategy rsi_macd --source kraken --symbol BTC/USD \\
        --timeframe 1d --start 2026-01-01 --end 2026-11-11 --rsi 14 --rsi-low 40 \\
        --rsi-high 70 --rsi-exit 75 --macd 12,26,9 --sl 0.05 --tp 0.10 --size 1.0 \\
        --capital 10 --fee-bp 40 --slip-bp 5 --daily-loss 0.15

紙上決策（同一根已收盤 K 線重跑不會再下單）：

    python -m ymzz.cli paper --strategy rsi_macd --source kraken --symbol BTC/USD --timeframe 1d
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from ymzz.data import load_kraken_limits, load_market
from ymzz.engine import CostConfig, EngineConfig, run_backtest, sample_buy_hold
from ymzz.errors import ConfigError, YmzzError
from ymzz.http_cache import DataClient
from ymzz.paper import step_decision
from ymzz.sim import build_runner
from ymzz.strategies import compute_signals, get_strategy
from ymzz.timeutil import now_utc, to_kst


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _parse_macd(text: str) -> tuple[float, float, float]:
    parts = [piece.strip() for piece in text.split(",")]
    if len(parts) != 3:
        raise ConfigError("MACD 參數要寫成 12,26,9")
    return tuple(float(piece) for piece in parts)  # type: ignore[return-value]


def strategy_params(strategy: str, args) -> dict:
    fast, slow, signal = _parse_macd(args.macd)
    if strategy == "rsi_macd":
        return {
            "rsi_period": args.rsi,
            "rsi_low": args.rsi_low,
            "rsi_high": args.rsi_high,
            "rsi_exit": args.rsi_exit,
            "macd_fast": fast,
            "macd_slow": slow,
            "macd_signal": signal,
        }
    if strategy == "macd":
        return {"fast": fast, "slow": slow, "signal": signal}
    if strategy == "rsi_single":
        return {"period": args.rsi}
    if strategy == "rsi_three":
        return {}
    return {}


def _stamp(moment: datetime | None = None) -> str:
    kst = to_kst(moment or now_utc())
    return kst.strftime("%m%d_%H%M")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def run_backtest_cli(args, root: Path | None = None) -> dict:
    root = root or project_root()
    if args.source not in {"kraken", "okx"}:
        raise ConfigError("這個指令的來源只接受 kraken 或 okx。失敗時不會改接別的交易所。")
    get_strategy(args.strategy)
    market = "crypto_spot"
    symbol = args.symbol
    if args.source == "kraken" and symbol.upper().replace("-", "/") not in {"BTC/USD", "XBT/USD"}:
        raise ConfigError("Kraken 這版只接 BTC/USD。")
    client = DataClient(root / "data" / "cache")
    spec = {
        "market": market,
        "source": args.source,
        "symbol": "BTC/USD" if args.source == "kraken" else symbol,
        "timeframe": args.timeframe,
        "start": args.start,
        "end": args.end,
        "data_mode": "live",
        "strict_range": True,
    }
    bars, funding, provenance = load_market(spec, client)
    closes = [bar.close for bar in bars]
    params = strategy_params(args.strategy, args)
    signals, indicators, warnings = compute_signals(args.strategy, closes, params, False)
    indicator_exits = indicators.pop("_indicator_exit", None)
    limits = None
    if args.source == "kraken":
        limits, limit_prov = load_kraken_limits(client, "live")
        provenance = {**provenance, "kraken_limits": limits, "kraken_limits_sha256": limit_prov.get("sha256")}
        ordermin = float(limits["ordermin"])
        costmin = float(limits["costmin"])
        lot_decimals = int(limits["lot_decimals"])
    else:
        ordermin = 0.0
        costmin = 0.0
        lot_decimals = None
        warnings.append("OKX 這次回測沒有查最小下單量，所以沒有套用最小量過濾。")
    halt = args.daily_loss if args.daily_loss and args.daily_loss > 0 else None
    result = run_backtest(
        bars,
        signals,
        funding,
        EngineConfig(
            capital=args.capital,
            leverage=1,
            instrument="spot",
            cost=CostConfig(fee_bps=args.fee_bp, slippage_bps=args.slip_bp),
            tp_pct=args.tp,
            sl_pct=args.sl,
            position_frac=args.size,
            daily_loss_halt_pct=halt,
            ordermin=ordermin,
            costmin=costmin,
            lot_decimals=lot_decimals if lot_decimals is not None else None,
            signal_mode="pulse" if indicator_exits is not None else "target",
        ),
        indicator_exits=indicator_exits,
    )
    held = sample_buy_hold(
        bars,
        args.capital,
        args.fee_bp,
        args.slip_bp,
        lot_decimals if lot_decimals is not None else 8,
        ordermin,
        costmin,
    )
    moment = now_utc()
    stamp = _stamp(moment)
    stem = f"{stamp}_seoul_{args.strategy}_{args.source}_{args.timeframe}"
    out = root / "results"
    ledger_rows = result.round_trips or [row for row in result.trades if row["action"] in {"buy", "sell", "skip"}]
    ledger_path = out / f"{stem}_ledger.csv"
    _write_csv(ledger_path, ledger_rows)
    report = _report(
        moment,
        args,
        bars,
        result,
        held,
        provenance,
        warnings,
        limits,
        ledger_path.name,
    )
    md_path = out / f"{stem}.md"
    md_path.write_text(report, encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / md_path.name).write_text(report, encoding="utf-8")
    summary = {
        "report": str(md_path),
        "ledger": str(ledger_path),
        "metrics": result.metrics,
        "buy_hold_return_pct": held.get("return_pct"),
        "bars": len(bars),
        "warnings": warnings + result.warnings,
    }
    print(report)
    return summary


def _report(moment, args, bars, result, held, provenance, warnings, limits, ledger_name) -> str:
    kst = to_kst(moment)
    m = result.metrics
    trips = m.get("round_trips") or 0
    win = m.get("win_rate")
    bh = held.get("return_pct")
    lines = [
        f"# {args.strategy} 回測（晏銘湛箴）",
        "",
        f"- 寫作時間：{kst.strftime('%Y-%m-%d %H:%M')} KST",
        f"- 來源：{args.source}  {args.symbol}  {args.timeframe}",
        f"- 區間：{args.start or '最早'} → {args.end or '最新已收盤'}",
        f"- 已收盤 K 線：{len(bars)}",
        f"- 本金 {args.capital}，倉位比例 {args.size}，槓桿 1（現貨）",
        f"- 手續費 {args.fee_bp} bp，滑點 {args.slip_bp} bp，止損 {args.sl}，止盈 {args.tp}，日虧熔斷 {args.daily_loss}",
        "- 這次回測沒有下單，也沒有讀取 API 密鑰。",
        "",
        "## 結果",
        "",
        f"- 來回成交 {trips} 筆，勝率 {('—' if win is None else f'{win * 100:.1f}%')}",
        f"- 期末權益 {m['final_equity']:.4f}，報酬 {_pct(m['total_return'])}，最大回撤 {_pct(m['max_drawdown'])}",
        f"- 手續費 {m['fees_paid']:.4f}，低於最小量而跳過 {m.get('skips_below_minimum', 0)}，日虧熔斷跳過 {m.get('skips_halt', 0)}",
        f"- 樣本期買入持有（第一根開盤到最後收盤，滿倉、同一套費用）："
        f"{'無法成交' if not held.get('possible') else f'{bh:.2f}%'}，裸價格 {held.get('raw_move_pct')}",
        "",
        "先前核准的 Kraken 日線、100% 倉位、同一組預設參數，在 2026-11-11 的快照是 5 筆、勝率 40%、10 美元到 11.03 美元（+10.27%）、最大回撤 5.36%、買入持有 -16.53%。",
        "若這次數字不同，以這份報告為準，不回寫成那組舊數字。5 筆交易不能證明策略有效；4 小時與 1 小時在那次檢查裡是虧錢的。",
        "",
        "## 資料",
        "",
        f"- 網址：{provenance.get('source_url')}",
        f"- 抓取：{provenance.get('fetched_at_kst')}（UTC {provenance.get('fetched_at_utc')}）",
        f"- sha256：{provenance.get('sha256')}",
        f"- 剔除未收盤：{provenance.get('dropped_unclosed')}",
        f"- 說明：{provenance.get('note_zh')}",
    ]
    if limits:
        lines.append(
            f"- Kraken AssetPairs：ordermin {limits.get('ordermin')}，costmin {limits.get('costmin')}，"
            f"小數位 {limits.get('lot_decimals')}，狀態 {limits.get('status')}，抓取 {limits.get('fetched_at_kst')}"
        )
    if warnings or result.warnings:
        lines.extend(["", "## 警告", ""])
        for text in warnings + result.warnings:
            lines.append(f"- {text}")
    lines.extend(["", "## 來回成交", "", f"帳本：`results/{ledger_name}`", ""])
    if result.round_trips:
        lines.append("| 進場 | 出場 | 原因 | 數量 | 進場成交 | 出場成交 | 損益 | 手續費 | 期末權益 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in result.round_trips:
            lines.append(
                f"| {row['entry_time']} | {row['exit_time']} | {row['reason']} | {row['qty']:.8f} | "
                f"{row['entry_fill']:.2f} | {row['exit_fill']:.2f} | {row['pnl']:.4f} | {row['fees']:.4f} | {row['equity_after']:.4f} |"
            )
    else:
        lines.append("沒有完整來回。逐筆帳在同一份 CSV。")
    lines.append("")
    return "\n".join(lines) + "\n"


def run_paper_cli(args, root: Path | None = None, runner=None) -> dict:
    root = root or project_root()
    if args.source != "kraken":
        raise ConfigError("紙上與實盤路徑只接 Kraken 現貨。")
    client = DataClient(root / "data" / "cache")
    spec = {
        "market": "crypto_spot",
        "source": "kraken",
        "symbol": "BTC/USD",
        "timeframe": args.timeframe,
        "start": None,
        "end": None,
        "data_mode": "live",
        "strict_range": False,
    }
    bars, _funding, _prov = load_market(spec, client)
    closes = [bar.close for bar in bars]
    params = strategy_params(args.strategy, args)
    signals, indicators, _warnings = compute_signals(args.strategy, closes, params, False)
    indicator_exits = indicators.pop("_indicator_exit", None) or [0.0] * len(bars)
    limits, _ = load_kraken_limits(client, "live")
    state_path = Path(args.state) if args.state else root / "data" / "paper" / "state.json"
    if not state_path.is_absolute():
        state_path = root / state_path
    log_stem = state_path.with_suffix("")
    decision = step_decision(
        bars=bars,
        signals=signals,
        indicator_exits=indicator_exits,
        indicators=indicators,
        state_path=state_path,
        csv_path=Path(str(log_stem) + "_log.csv"),
        md_path=Path(str(log_stem) + "_log.md"),
        capital=args.capital,
        position_frac=args.size,
        sl_pct=args.sl,
        tp_pct=args.tp,
        daily_loss_halt_pct=args.daily_loss if args.daily_loss and args.daily_loss > 0 else None,
        ordermin=float(limits["ordermin"]),
        costmin=float(limits["costmin"]),
        lot_decimals=int(limits["lot_decimals"]),
        symbol=args.symbol,
        live=args.live,
        confirm=args.i_confirm_live,
        leverage=1.0,
        runner=runner or build_runner(
            fee_bp=args.fee_bp,
            slip_bp=args.slip_bp,
            ledger_path=Path(str(log_stem) + "_sim.jsonl"),
        ),
    )
    print(
        f"{decision.get('action')}  {decision.get('reason')}  "
        f"idempotent={decision.get('idempotent')}  bar={decision.get('bar_open')}"
    )
    return decision


def run_dashboard_cli(args) -> None:
    import uvicorn

    from ymzz.dashboard import create_dashboard_app

    uvicorn.run(create_dashboard_app(), host=args.host, port=args.port, log_level="info")


def run_daemon_cli(args, root: Path | None = None, step=None, sleep=None, cycles: int | None = None) -> dict:
    from ymzz.runner import run_cycles

    root = root or project_root()
    if args.mode == "live":
        args.live = True
    else:
        args.live = False
        args.i_confirm_live = False
    if args.state is None:
        folder = "live" if args.live else "paper"
        args.state = str(root / "data" / folder / "state.json")
    args.source = "kraken"
    if step is None:
        step = lambda paper_args: run_paper_cli(paper_args, root)
    lock = root / "data" / "runner" / "ymzz.lock"
    beat = root / "data" / "runner" / "heartbeat.json"
    if args.once:
        cycles = 1 if cycles is None else cycles
    return run_cycles(
        args=args,
        step=step,
        sleep=sleep or __import__("time").sleep,
        lock_path=lock,
        heartbeat_path=beat,
        cycles=cycles,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ymzz.cli", description="晏銘湛箴研究指令。回測不下單。")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target):
        target.add_argument("--strategy", required=True)
        target.add_argument("--source", required=True, choices=["kraken", "okx"])
        target.add_argument("--symbol", required=True)
        target.add_argument("--timeframe", default="1d", choices=["1d", "4h", "1h"])
        target.add_argument("--rsi", type=float, default=14)
        target.add_argument("--rsi-low", type=float, default=40, dest="rsi_low")
        target.add_argument("--rsi-high", type=float, default=70, dest="rsi_high")
        target.add_argument("--rsi-exit", type=float, default=75, dest="rsi_exit")
        target.add_argument("--macd", default="12,26,9")
        target.add_argument("--sl", type=float, default=0.05)
        target.add_argument("--tp", type=float, default=0.10)
        target.add_argument("--size", type=float, default=1.0)
        target.add_argument("--capital", type=float, default=10)
        target.add_argument("--fee-bp", type=float, default=40, dest="fee_bp")
        target.add_argument("--slip-bp", type=float, default=5, dest="slip_bp")
        target.add_argument("--daily-loss", type=float, default=0.15, dest="daily_loss")

    backtest = sub.add_parser("backtest", help="用公開行情回測，寫出帳本與首爾時間報告")
    add_common(backtest)
    backtest.add_argument("--start", default="2026-01-01")
    backtest.add_argument("--end", default=None)

    paper = sub.add_parser("paper", help="對最新已收盤 K 線做一次紙上決策")
    add_common(paper)
    paper.add_argument("--state", default=None)
    paper.add_argument("--live", action="store_true", help="改送實盤單。預設拒絕。")
    paper.add_argument("--i-confirm-live", action="store_true", dest="i_confirm_live")

    run = sub.add_parser("run", help="常駐：心跳，並在新的已收盤 K 線上做一次決策")
    run.add_argument("--mode", choices=["paper", "live"], default="paper")
    run.add_argument("--i-confirm-live", action="store_true", dest="i_confirm_live")
    run.add_argument("--interval", type=float, default=180)
    run.add_argument("--once", action="store_true")
    run.add_argument("--state", default=None)
    run.add_argument("--strategy", default="rsi_macd")
    run.add_argument("--symbol", default="BTC/USD")
    run.add_argument("--timeframe", default="1d", choices=["1d", "4h", "1h"])
    run.add_argument("--rsi", type=float, default=14)
    run.add_argument("--rsi-low", type=float, default=40, dest="rsi_low")
    run.add_argument("--rsi-high", type=float, default=70, dest="rsi_high")
    run.add_argument("--rsi-exit", type=float, default=75, dest="rsi_exit")
    run.add_argument("--macd", default="12,26,9")
    run.add_argument("--sl", type=float, default=0.05)
    run.add_argument("--tp", type=float, default=0.10)
    run.add_argument("--size", type=float, default=1.0)
    run.add_argument("--capital", type=float, default=10)
    run.add_argument("--fee-bp", type=float, default=40, dest="fee_bp")
    run.add_argument("--slip-bp", type=float, default=5, dest="slip_bp")
    run.add_argument("--daily-loss", type=float, default=0.15, dest="daily_loss")

    dashboard = sub.add_parser("dashboard", help="本機多端儀表臺，預設 127.0.0.1:8787")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8787)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backtest":
            run_backtest_cli(args)
        elif args.command == "paper":
            run_paper_cli(args)
        elif args.command == "dashboard":
            run_dashboard_cli(args)
        elif args.command == "run":
            run_daemon_cli(args)
        else:
            parser.error("未知指令")
    except YmzzError as exc:
        print(exc.message_zh, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
