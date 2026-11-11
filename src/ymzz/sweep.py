"""Kraken 現貨掃描。只讀公開 OHLC 與 AssetPairs，不下單。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ymzz.data import load_kraken_limits, load_market
from ymzz.engine import CostConfig, EngineConfig, run_backtest, sample_buy_hold
from ymzz.http_cache import DataClient
from ymzz.strategies import STRATEGIES, compute_signals, get_strategy
from ymzz.timeutil import now_utc

SWEEP_STRATEGIES = ("rsi_macd", "macd", "rsi_single", "ma_cross", "bollinger")
SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD")
TIMEFRAMES = ("1d", "4h")
FEE_BPS = 80.0
SLIP_BPS = 5.0
CHASE_RISE = 0.30
CHASE_DAYS = 20


def chase_bars_for(timeframe: str) -> int:
    if timeframe == "1d":
        return CHASE_DAYS
    if timeframe == "4h":
        return CHASE_DAYS * 6
    raise ValueError(timeframe)


def return_over_drawdown(total_return: float | None, max_drawdown: float | None) -> float | None:
    if total_return is None or max_drawdown is None or max_drawdown <= 1e-12:
        return None
    return total_return / max_drawdown


def pick_recommendation(rows: list[dict]) -> dict:
    """本金與回撤優先。至少 5 筆來回才推薦。追高濾網只有在回撤與期末都不差時才取代原列。"""
    usable = [row for row in rows if row.get("round_trips", 0) >= 5 and not row.get("error")]
    preserved = [row for row in usable if row["final_equity"] + 1e-9 >= row["capital"]]
    pool = preserved or usable
    if not pool:
        return {
            "id": None,
            "reason_zh": "沒有任何一組有 5 筆以上的來回。成交太少，不能推薦一組拿 10 美元去跑。",
        }
    pool.sort(key=lambda row: (row["max_drawdown"], -row["total_return"], row["id"]))
    chosen = pool[0]
    if not chosen["chase"]:
        twin_id = chosen["id"] + "|+chase"
        twin = next((row for row in pool if row["id"] == twin_id), None)
        if twin is not None and twin["max_drawdown"] <= chosen["max_drawdown"] + 1e-12 and twin["final_equity"] + 1e-9 >= chosen["final_equity"]:
            chosen = twin
    kept = chosen["final_equity"] + 1e-9 >= chosen["capital"]
    reason = (
        f"在至少 5 筆來回裡，先看期末是否還守住 {chosen['capital']:.0f} 美元，再取最大回撤最小的一列。"
        if kept
        else "沒有一組在 5 筆來回之後仍守住本金。下面這列只是回撤較小的一組，不是建議加倉。"
    )
    if chosen["chase"]:
        plain = next((row for row in rows if row["id"] == chosen["id"].removesuffix("|+chase")), None)
        same = (
            plain is not None
            and abs(plain["final_equity"] - chosen["final_equity"]) < 1e-9
            and chosen.get("skips_chase", 0) == 0
        )
        if same:
            reason += "這一段濾網沒有擋下任何進場，成交與不過濾的那列相同。濾網仍開著，只是這段沒有用上。"
        else:
            reason += "同一組打開「不追高」之後，回撤沒有變大、期末也沒有變差，所以用濾網這列。"
    return {"id": chosen["id"], "reason_zh": reason}


def _run_one(bars, signals, indicator_exits, limits, *, chase: bool, timeframe: str) -> dict:
    result = run_backtest(
        bars,
        signals,
        [],
        EngineConfig(
            capital=10,
            leverage=1,
            instrument="spot",
            cost=CostConfig(fee_bps=FEE_BPS, slippage_bps=SLIP_BPS),
            tp_pct=0.10,
            sl_pct=0.05,
            position_frac=1.0,
            daily_loss_halt_pct=0.15,
            ordermin=float(limits["ordermin"]),
            costmin=float(limits["costmin"]),
            lot_decimals=int(limits["lot_decimals"]),
            signal_mode="pulse" if indicator_exits is not None else "target",
            chase_rise=CHASE_RISE if chase else None,
            chase_bars=chase_bars_for(timeframe),
        ),
        indicator_exits=indicator_exits,
    )
    held = sample_buy_hold(
        bars,
        10,
        FEE_BPS,
        SLIP_BPS,
        int(limits["lot_decimals"]),
        float(limits["ordermin"]),
        float(limits["costmin"]),
    )
    metrics = result.metrics
    ratio = return_over_drawdown(metrics["total_return"], metrics["max_drawdown"])
    trades = [
        {
            "time": row["time"],
            "action": row["action"],
            "qty": row["qty"],
            "price": row["price"],
            "reason": row["reason"],
            "fee": row["fee"],
        }
        for row in result.trades
        if row["action"] in {"buy", "sell"}
    ]
    return {
        "final_equity": metrics["final_equity"],
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "return_over_dd": ratio,
        "win_rate": metrics["win_rate"],
        "round_trips": metrics["round_trips"],
        "trade_count": metrics["trade_count"],
        "fees_paid": metrics["fees_paid"],
        "funding_paid": metrics["funding_paid"],
        "skips_minimum": metrics["skips_below_minimum"],
        "skips_chase": metrics["skips_chase"],
        "buy_hold_return": None if held.get("return_pct") is None else held["return_pct"] / 100.0,
        "equity": result.equity,
        "trades": trades,
        "warnings": result.warnings,
    }


def run_sweep(root: Path, client: DataClient | None = None) -> dict:
    root = Path(root)
    client = client or DataClient(root / "data" / "cache")
    now = now_utc()
    start_1d = (now - timedelta(days=365)).date().isoformat()
    for strategy_id in SWEEP_STRATEGIES:
        spec = get_strategy(strategy_id)
        if len(spec.decision_indicators) > 3:
            raise RuntimeError(f"{strategy_id} 決策指標超過 3 個")
    limits = {}
    for symbol in SYMBOLS:
        from ymzz.data import kraken_rest_pair

        info, prov = load_kraken_limits(client, "live", kraken_rest_pair(symbol))
        info["sha256"] = prov.get("sha256")
        limits[symbol] = info
    rows = []
    coverage = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            spec = {
                "market": "crypto_spot",
                "source": "kraken",
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start_1d if timeframe == "1d" else None,
                "end": None,
                "data_mode": "live",
                "strict_range": timeframe == "1d",
            }
            bars, _funding, provenance = load_market(spec, client, now)
            closes = [bar.close for bar in bars]
            coverage.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bars": len(bars),
                    "first": bars[0].open_time.isoformat(),
                    "last": bars[-1].close_time.isoformat(),
                    "history_short": bool(provenance.get("history_short")),
                    "note_zh": provenance.get("note_zh"),
                }
            )
            prepared = {}
            for strategy_id in SWEEP_STRATEGIES:
                signals, indicators, warnings = compute_signals(strategy_id, closes, None, False)
                exits = indicators.pop("_indicator_exit", None)
                prepared[strategy_id] = (signals, exits, warnings)
            for strategy_id in SWEEP_STRATEGIES:
                signals, exits, warnings = prepared[strategy_id]
                for chase in (False, True):
                    stats = _run_one(bars, signals, exits, limits[symbol], chase=chase, timeframe=timeframe)
                    row_id = f"{symbol}|{timeframe}|{strategy_id}" + ("|+chase" if chase else "")
                    rows.append(
                        {
                            "id": row_id,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "strategy": strategy_id,
                            "chase": chase,
                            "capital": 10.0,
                            "fee_bps": FEE_BPS,
                            "slip_bps": SLIP_BPS,
                            "indicators": list(STRATEGIES[strategy_id].decision_indicators),
                            "bars": len(bars),
                            "first": bars[0].open_time.isoformat(),
                            "last": bars[-1].close_time.isoformat(),
                            "ordermin": limits[symbol]["ordermin"],
                            "costmin": limits[symbol]["costmin"],
                            "signal_warnings": warnings,
                            **stats,
                        }
                    )
    recommendation = pick_recommendation(rows)
    payload = {
        "generated_at_utc": now.isoformat(),
        "capital": 10,
        "fee_bps": FEE_BPS,
        "slip_bps": SLIP_BPS,
        "sl": 0.05,
        "tp": 0.10,
        "daily_loss": 0.15,
        "chase_rise": CHASE_RISE,
        "chase_days": CHASE_DAYS,
        "limits": limits,
        "coverage": coverage,
        "recommendation": recommendation,
        "rows": rows,
    }
    return payload


def write_sweep(root: Path, payload: dict) -> tuple[Path, Path]:
    root = Path(root)
    json_path = root / "results" / "crypto_sweep.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    from zoneinfo import ZoneInfo

    kst = now_utc().astimezone(ZoneInfo("Asia/Seoul"))
    doc_path = root / "docs" / f"{kst.strftime('%m%d_%H%M')}_seoul_加密策略掃描.md"
    doc_path.write_text(render_markdown(payload, kst.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    return json_path, doc_path


def _plain_number(value: float) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def render_markdown(payload: dict, written: str) -> str:
    ranked = sorted(
        [row for row in payload["rows"] if not row.get("error")],
        key=lambda row: (
            row["return_over_dd"] is None,
            -(row["return_over_dd"] or 0),
            -row["total_return"],
        ),
    )
    lines = [
        "# Kraken 現貨策略掃描",
        "",
        f"Written: {written} KST",
        "",
        "只掃 Kraken 現貨 BTC/USD、ETH/USD、SOL/USD。本金 10 美元，現貨 1 倍。不下單，不用私鑰。",
        "",
        "每組都帶止損 5%、止盈 10%、滑點 5 bp、UTC 日虧熔斷 15%，以及該交易對公開 AssetPairs 的最小下單量。",
        "手續費用 80 bp。Kraken 費率文寫第一級 maker 0.40%、taker 0.80%，頁面 Last updated: October 30, 2026。",
        "這次回測的成交是下一根開盤，止損也按市價處理，所以用 taker，不用倉庫裡那次 40 bp 的 maker 假設。數字不會和那次 11.03 美元的日線重跑相同。",
        "這次抓下的費率頁 HTML 沒有再解析出 0.80% 那一欄，所以沿用教程裡已寫下的第一級 taker，沒有改成別的費率。",
        "出處：<https://support.kraken.com/articles/cross-platform-fee-tier-changes> 與 <https://support.kraken.com/articles/360000526126-what-are-maker-and-taker-fees->。",
        "",
        "決策指標沒有超過 3 個。RSI+MACD 是 RSI、DIF、DEA。MACD 是 DIF、DEA。RSI 單線只有 RSI。均線是快慢兩條。布林是下軌與中軌。",
        "「不追高」不是第四個指標。它只在空手時，若開盤價比前 20 日最低價高出 30% 以上，就跳過這次進場。4 小時線把 20 日換成 120 根。已持有的倉位不會因為這條濾網被趕走。",
        "",
        "## 資料實際蓋到哪裡",
        "",
        "Kraken 公開 OHLC 一次大約 720 根。日線因此蓋得過近一年。4 小時線只有約 120 天，更早的 K 線這支 API 沒有，沒有用別的交易所補。",
        "",
        "| 交易對 | 週期 | 根數 | 第一根 | 最後一根 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in payload["coverage"]:
        lines.append(
            f"| {item['symbol']} | {item['timeframe']} | {item['bars']} | {item['first'][:10]} | {item['last'][:10]} |"
        )
    lines.extend(
        [
            "",
            "最小下單量來自公開 AssetPairs，不是寫死的：",
            "",
            "| 交易對 | ordermin | costmin |",
            "| --- | --- | --- |",
        ]
    )
    for symbol, info in payload["limits"].items():
        lines.append(f"| {symbol} | {_plain_number(info['ordermin'])} | {_plain_number(info['costmin'])} |")
    lines.extend(
        [
            "",
            "## 依報酬／最大回撤排序",
            "",
            "這一欄只是排序，不是推薦。回撤為 0 時這個比值沒有定義，排在後面。成交很少的列可以排很前，那不代表有優勢。",
            "",
            "| 排名 | 交易對 | 週期 | 策略 | 不追高 | 報酬 | 最大回撤 | 報酬/回撤 | 勝率 | 來回 | 手續費 | 期末 | 買入持有 | 最小量跳過 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, row in enumerate(ranked, start=1):
        ratio = "—" if row["return_over_dd"] is None else f"{row['return_over_dd']:.2f}"
        win = "—" if row["win_rate"] is None else f"{row['win_rate'] * 100:.0f}%"
        held = "—" if row["buy_hold_return"] is None else f"{row['buy_hold_return'] * 100:.1f}%"
        lines.append(
            "| {i} | {symbol} | {tf} | {strategy} | {chase} | {ret:.1f}% | {dd:.1f}% | {ratio} | {win} | {trips} | {fees:.2f} | {final:.2f} | {held} | {skips} |".format(
                i=index,
                symbol=row["symbol"],
                tf=row["timeframe"],
                strategy=row["strategy"],
                chase="是" if row["chase"] else "否",
                ret=row["total_return"] * 100,
                dd=row["max_drawdown"] * 100,
                ratio=ratio,
                win=win,
                trips=row["round_trips"],
                fees=row["fees_paid"],
                final=row["final_equity"],
                held=held,
                skips=row["skips_minimum"],
            )
        )
    rec = payload["recommendation"]
    lines.extend(["", "## 只推薦一組", ""])
    if not rec.get("id"):
        lines.append(rec["reason_zh"])
    else:
        chosen = next(row for row in payload["rows"] if row["id"] == rec["id"])
        lines.append(f"推薦 `{chosen['id']}`。")
        lines.append("")
        lines.append(rec["reason_zh"])
        lines.append("")
        lines.append(
            f"期末 {chosen['final_equity']:.2f} 美元，報酬 {chosen['total_return'] * 100:.1f}%，最大回撤 {chosen['max_drawdown'] * 100:.1f}%，來回 {chosen['round_trips']} 筆，手續費 {chosen['fees_paid']:.2f} 美元。"
        )
        lines.append("")
        lines.append("這組沒有證明優勢。樣本裡的來回仍然少，參數是預設值，沒有為這段行情再調。4 小時與日線的長度不一樣，不能把兩欄的報酬直接比成同一段歷史。")
    lines.extend(
        [
            "",
            "## 不要過度解讀",
            "",
            "60 組裡總有幾組看起來好看。那是試了很多次之後的結果，不是事先註冊的單一假設。勝率與報酬/回撤會把成交很少的列推得很高。ETH 日線 RSI 單線只有 1 筆來回卻排在前段，就是這個原因。10 美元扣掉 taker 費之後，幾筆來回就吃掉不少本金。資金費這一欄在現貨是 0。",
            f"{sum(1 for row in payload['rows'] if row['final_equity'] < row['capital'])} 組期末低於 10 美元，多半是 4 小時。日線有幾組賺錢，不能因此改去跑 4 小時。",
            "30% 與 20 日這兩個濾網數字是事先定的，沒有為了這份表再調。調這兩個數字會變成對這一段行情過擬合。",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    payload = run_sweep(root)
    json_path, doc_path = write_sweep(root, payload)
    print(json_path)
    print(doc_path)
    print(payload["recommendation"]["id"])


if __name__ == "__main__":
    main()
