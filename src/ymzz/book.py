"""每筆保證金 10 美元的樣本外比較。現貨只做多 1 倍。空單與 2 倍只走永續回測，不下實盤。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from ymzz.data import load_kraken_futures, load_kraken_limits, load_market
from ymzz.engine import CostConfig, EngineConfig, run_backtest
from ymzz.http_cache import DataClient
from ymzz.strategies import STRATEGIES, compute_signals, get_strategy
from ymzz.timeutil import now_utc

UTC = timezone.utc
TRAIN_START = date(2025, 10, 30)
TRAIN_END = date(2026, 4, 30)
OOS_START = date(2026, 5, 1)
BASELINE_START = date(2025, 11, 11)
CHASE = 0.30
CHASE_BARS = 20
SLIP = 5.0
GLOBAL_DD = 0.15
FLOOR = 0.5


def shift_signals(signals: list[float], delay: int) -> list[float]:
    if delay <= 0:
        return list(signals)
    return [0.0] * delay + list(signals[:-delay])


def cagr(start_equity: float, end_equity: float, days: float) -> float | None:
    if start_equity <= 0 or end_equity <= 0 or days <= 0:
        return None
    return (end_equity / start_equity) ** (365.25 / days) - 1.0


def losing_streak(trips: list[dict]) -> int:
    worst = current = 0
    for trip in trips:
        if trip["pnl"] < 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def survival(equity: list[dict], floor: float = FLOOR) -> dict:
    if not equity:
        return {"days": 0, "survived": False, "min_equity": None, "broke_on": None}
    start = datetime.fromisoformat(equity[0]["time"])
    minimum = min(row["equity"] for row in equity)
    for row in equity:
        if row["equity"] < floor:
            moment = datetime.fromisoformat(row["time"])
            return {
                "days": (moment - start).days,
                "survived": False,
                "min_equity": minimum,
                "broke_on": row["time"][:10],
            }
    end = datetime.fromisoformat(equity[-1]["time"])
    return {"days": (end - start).days, "survived": True, "min_equity": minimum, "broke_on": None}


def year_slices(equity: list[dict]) -> list[dict]:
    if not equity:
        return []
    buckets: dict[str, list[float]] = {}
    for row in equity:
        buckets.setdefault(row["time"][:4], []).append(row["equity"])
    rows = []
    previous = equity[0]["equity"]
    for year in sorted(buckets):
        last = buckets[year][-1]
        rows.append(
            {
                "year": year,
                "bars": len(buckets[year]),
                "return": (last / previous - 1.0) if previous else None,
                "end_equity": last,
            }
        )
        previous = last
    return rows


def choose_row(rows: list[dict]) -> dict | None:
    usable = [
        row
        for row in rows
        if row.get("round_trips", 0) >= 3 and row["final_equity"] + 1e-9 >= row["capital"] and row.get("survived")
    ]
    if not usable:
        return None
    usable.sort(key=lambda row: (row["max_drawdown"], -row["final_equity"], row["id"]))
    return usable[0]


def _window(bars, signals, exits, funding, start: date | None, end: date | None):
    kept_bars = []
    kept_signals = []
    kept_exits = [] if exits is not None else None
    for i, bar in enumerate(bars):
        day = bar.open_time.date()
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        kept_bars.append(bar)
        kept_signals.append(signals[i])
        if kept_exits is not None:
            kept_exits.append(exits[i])
    if not kept_bars:
        return kept_bars, kept_signals, kept_exits, []
    first, last = kept_bars[0].open_time, kept_bars[-1].close_time
    kept_funding = [item for item in funding if first <= item.time < last]
    return kept_bars, kept_signals, kept_exits, kept_funding


def _run(bars, signals, funding, exits, limits, *, fee, leverage, instrument, allow_short, sl, tp, delay=0, block_after=None) -> dict:
    signals = shift_signals(signals, delay)
    if exits is not None:
        exits = shift_signals(exits, delay)
    if instrument == "perpetual" and not funding:
        raise RuntimeError("永續這段沒有資金費，沒有用 0 填")
    result = run_backtest(
        bars,
        signals,
        funding if instrument == "perpetual" else [],
        EngineConfig(
            capital=10,
            leverage=leverage,
            instrument=instrument,
            cost=CostConfig(fee_bps=fee, slippage_bps=SLIP),
            tp_pct=tp,
            sl_pct=sl,
            position_frac=1.0,
            daily_loss_halt_pct=0.15,
            ordermin=float(limits.get("ordermin") or 0),
            costmin=float(limits.get("costmin") or 0),
            lot_decimals=limits.get("lot_decimals"),
            signal_mode="pulse" if exits is not None else "target",
            allow_short=allow_short,
            chase_rise=CHASE,
            chase_bars=CHASE_BARS,
            mmr=0.005 if instrument == "perpetual" else 0.004,
            block_entries_after=block_after,
            require_stop=True,
        ),
        indicator_exits=exits,
    )
    metrics = result.metrics
    buys = [row for row in result.trades if row["action"] in {"buy", "sell"} and row["reason"] == "signal" and row["position_after"] != 0]
    opening = [row for row in result.trades if row["reason"] == "signal" and abs(row["position_after"]) > abs(row["position_after"] - row["qty"]) - 1e-9]
    max_open = 0.0
    for row in result.trades:
        if row["action"] not in {"buy", "sell"} or row["reason"] != "signal":
            continue
        if abs(row["position_after"]) + 1e-12 >= abs(row["position_after"] - row["qty"]):
            max_open = max(max_open, abs(row["qty"]) * row["price"])
    life = survival(result.equity)
    span_days = 0
    if result.equity:
        span_days = (
            datetime.fromisoformat(result.equity[-1]["time"]) - datetime.fromisoformat(result.equity[0]["time"])
        ).days
    return {
        "final_equity": metrics["final_equity"],
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "round_trips": metrics["round_trips"],
        "win_rate": metrics["win_rate"],
        "fees_paid": metrics["fees_paid"],
        "funding_paid": metrics["funding_paid"],
        "liquidations": metrics["liquidations"],
        "capital": 10.0,
        "survived": life["survived"],
        "survival_days": life["days"],
        "broke_on": life["broke_on"],
        "min_equity": life["min_equity"],
        "worst_losing_streak": losing_streak(result.round_trips),
        "cagr": cagr(10.0, metrics["final_equity"], span_days),
        "span_days": span_days,
        "year_slices": year_slices(result.equity),
        "max_entry_notional": max_open,
        "equity": result.equity,
        "skips_chase": metrics["skips_chase"],
        "warnings": result.warnings,
        "_opening": len(opening),
        "_buys": len(buys),
    }


def _candidates():
    sls = (0.05, 0.08)
    tps = (0.10, 0.15)
    for period in (10, 14):
        for low in (35, 40):
            for exit_level in (75, 80):
                for fast, slow, signal in ((12, 26, 9), (8, 21, 5)):
                    for sl in sls:
                        for tp in tps:
                            yield (
                                "rsi_macd",
                                {
                                    "rsi_period": period,
                                    "rsi_low": low,
                                    "rsi_high": 70,
                                    "rsi_exit": exit_level,
                                    "macd_fast": fast,
                                    "macd_slow": slow,
                                    "macd_signal": signal,
                                },
                                sl,
                                tp,
                            )
    for fast in (5, 10):
        for slow in (20, 40):
            for sl in sls:
                for tp in tps:
                    yield ("ma_cross", {"fast": fast, "slow": slow}, sl, tp)
    for fast, slow, signal in ((12, 26, 9), (8, 21, 5)):
        for sl in sls:
            for tp in tps:
                yield ("macd", {"fast": fast, "slow": slow, "signal": signal}, sl, tp)


def _prepare(closes, strategy, params, allow_short):
    get_strategy(strategy)
    if len(STRATEGIES[strategy].decision_indicators) > 3:
        raise RuntimeError(strategy)
    signals, indicators, warnings = compute_signals(strategy, closes, params, allow_short)
    exits = indicators.pop("_indicator_exit", None)
    return signals, exits, warnings


def _public(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in {"equity", "warnings"}}


def run_study(root: Path, client: DataClient | None = None) -> dict:
    root = Path(root)
    client = client or DataClient(root / "data" / "cache")
    now = now_utc()
    spot = {}
    limits = {}
    from ymzz.data import kraken_rest_pair

    for symbol in ("BTC/USD", "ETH/USD", "SOL/USD", "BTC/USDC", "ETH/USDC", "SOL/USDC"):
        bars, _funding, prov = load_market(
            {
                "market": "crypto_spot",
                "source": "kraken",
                "symbol": symbol,
                "timeframe": "1d",
                "start": "2024-11-21",
                "end": None,
                "data_mode": "live",
                "strict_range": False,
            },
            client,
            now,
        )
        info, _ = load_kraken_limits(client, "live", kraken_rest_pair(symbol))
        spot[symbol] = bars
        limits[symbol] = info
        limits[symbol]["history_note"] = prov.get("note_zh")
    futures = {}
    for symbol in ("BTC/USD", "ETH/USD", "SOL/USD"):
        bars, funding, extra = load_kraken_futures(client, symbol, "1d", now)
        futures[symbol] = {"bars": bars, "funding": funding, "extra": extra}

    btc = spot["BTC/USD"]
    btc_closes = [bar.close for bar in btc]
    train_rows = []
    for strategy, params, sl, tp in _candidates():
        signals, exits, _warnings = _prepare(btc_closes, strategy, params, False)
        piece = _window(btc, signals, exits, [], TRAIN_START, TRAIN_END)
        if len(piece[0]) < 40:
            continue
        stats = _run(piece[0], piece[1], [], piece[2], limits["BTC/USD"], fee=80, leverage=1, instrument="spot", allow_short=False, sl=sl, tp=tp)
        train_rows.append(
            {
                "id": f"{strategy}|{params}|sl={sl}|tp={tp}",
                "strategy": strategy,
                "params": params,
                "sl": sl,
                "tp": tp,
                **_public(stats),
            }
        )
    selected = choose_row(train_rows)
    if selected is None:
        selected = {
            "id": "rsi_macd|defaults",
            "strategy": "rsi_macd",
            "params": None,
            "sl": 0.05,
            "tp": 0.10,
            "reason": "train_empty",
        }
    else:
        selected = dict(selected)
        selected["reason"] = "train_rule"

    def evaluate(symbol, bars, funding, limits_row, *, fee, leverage, instrument, allow_short, start, end):
        closes = [bar.close for bar in bars]
        signals, exits, _warnings = _prepare(closes, selected["strategy"], selected["params"], allow_short)
        piece = _window(bars, signals, exits, funding, start, end)
        stats = _run(
            piece[0],
            piece[1],
            piece[3],
            piece[2],
            limits_row,
            fee=fee,
            leverage=leverage,
            instrument=instrument,
            allow_short=allow_short,
            sl=selected["sl"],
            tp=selected["tp"],
        )
        return stats

    variants = []
    specs = (
        ("spot-long-1x", "spot", 1, False, "BTC/USD"),
        ("perp-long-1x", "perpetual", 1, False, "BTC/USD"),
        ("perp-long-2x", "perpetual", 2, False, "BTC/USD"),
        ("perp-both-1x", "perpetual", 1, True, "BTC/USD"),
        ("perp-both-2x", "perpetual", 2, True, "BTC/USD"),
    )
    for fee in (80, 40):
        for name, instrument, leverage, allow_short, symbol in specs:
            if instrument == "spot":
                bars, funding, limits_row = spot[symbol], [], limits[symbol]
            else:
                bars, funding = futures[symbol]["bars"], futures[symbol]["funding"]
                decimals = futures[symbol]["extra"]["lot_decimals"]
                limits_row = {"ordermin": 10 ** (-decimals), "costmin": 0, "lot_decimals": decimals}
            stats = evaluate(
                symbol,
                bars,
                funding,
                limits_row,
                fee=fee,
                leverage=leverage,
                instrument=instrument,
                allow_short=allow_short,
                start=OOS_START,
                end=None,
            )
            variants.append(
                {
                    "id": f"{name}|fee={fee}",
                    "name": name,
                    "fee_bps": fee,
                    "instrument": instrument,
                    "leverage": leverage,
                    "allow_short": allow_short,
                    "symbol": symbol,
                    **_public(stats),
                    "equity": stats["equity"] if fee == 80 else [],
                }
            )

    baseline = evaluate(
        "BTC/USD",
        spot["BTC/USD"],
        [],
        limits["BTC/USD"],
        fee=80,
        leverage=1,
        instrument="spot",
        allow_short=False,
        start=BASELINE_START,
        end=None,
    )
    # 樣本外只在 80 bp 裡挑。40 bp 是同一組參數的對照，不另選。
    oos80 = [row for row in variants if row["fee_bps"] == 80]
    recommended = choose_row(oos80)
    if recommended is None:
        recommended = {"id": None, "reason_zh": "樣本外沒有一組同時守住 10 美元、活過這段、而且至少 3 筆來回。不能為了成長把回撤較大的一組說成穩。"}
    else:
        recommended = {
            "id": recommended["id"],
            "reason_zh": "樣本外先留下期末仍至少 10 美元、權益沒有跌破 0.5、來回至少 3 筆的組，再取最大回撤最小的。2 倍或做空只有回撤沒有更差才會被留下。",
        }

    chosen = next((row for row in oos80 if row["id"] == recommended.get("id")), None)
    slots = []
    aggregate = None
    if chosen is not None:
        use_perp = chosen["instrument"] == "perpetual"
        symbols = ("BTC/USD", "ETH/USD", "SOL/USD") if use_perp else ("BTC/USD", "ETH/USD", "SOL/USD", "BTC/USDC", "ETH/USDC", "SOL/USDC")
        delays = (0, 5, 10)
        prepared = []
        for symbol in symbols:
            if use_perp:
                bars = futures[symbol]["bars"]
                funding = futures[symbol]["funding"]
                decimals = futures[symbol]["extra"]["lot_decimals"]
                limits_row = {"ordermin": 10 ** (-decimals), "costmin": 0, "lot_decimals": decimals}
            else:
                bars = spot[symbol]
                funding = []
                limits_row = limits[symbol]
            closes = [bar.close for bar in bars]
            signals, exits, _warnings = _prepare(closes, selected["strategy"], selected["params"], chosen["allow_short"])
            for delay in delays:
                prepared.append((symbol, delay, bars, signals, exits, funding, limits_row))

        def once(block_after=None):
            rows = []
            for symbol, delay, bars, signals, exits, funding, limits_row in prepared:
                piece = _window(bars, signals, exits, funding, OOS_START, None)
                stats = _run(
                    piece[0],
                    piece[1],
                    piece[3],
                    piece[2],
                    limits_row,
                    fee=80,
                    leverage=chosen["leverage"],
                    instrument=chosen["instrument"],
                    allow_short=chosen["allow_short"],
                    sl=selected["sl"],
                    tp=selected["tp"],
                    delay=delay,
                    block_after=block_after,
                )
                rows.append({"id": f"{symbol}|delay={delay}", "symbol": symbol, "delay": delay, **stats})
            return rows

        first = once()
        agg_curve, halt = _aggregate(first)
        if halt is not None:
            halt_at = datetime.fromisoformat(halt + "T00:00:00+00:00")
            first = once(halt_at)
            agg_curve, _again = _aggregate(first)
        slots = [_public(row) | {"equity": row["equity"]} for row in first]
        aggregate = {
            "equity": agg_curve,
            "final_equity": agg_curve[-1]["equity"] if agg_curve else None,
            "max_drawdown": max((row["drawdown"] for row in agg_curve), default=None),
            "capital": 10 * len(first),
            "slot_count": len(first),
            "global_halt": halt,
            "survival": survival(agg_curve, floor=FLOOR * len(first)),
            "cagr": cagr(10 * len(first), agg_curve[-1]["equity"], (datetime.fromisoformat(agg_curve[-1]["time"]) - datetime.fromisoformat(agg_curve[0]["time"])).days) if agg_curve else None,
        }

    for row in variants:
        cap = 20.0 if row["leverage"] == 2 else 10.0
        if row["max_entry_notional"] > cap + 0.05:
            raise RuntimeError(f"{row['id']} 進場名義 {row['max_entry_notional']} 超過 {cap}")

    return {
        "generated_at_utc": now.isoformat(),
        "caps": {"max_margin_usd": 10, "max_notional_usd": 20, "spot_notional_cap": 10},
        "windows": {
            "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
            "oos": [OOS_START.isoformat(), None],
            "funding_first": futures["BTC/USD"]["extra"]["funding_first"],
            "funding_last": futures["BTC/USD"]["extra"]["funding_last"],
            "funding_points": futures["BTC/USD"]["extra"]["funding_points"],
        },
        "selected_on_train": {key: selected[key] for key in ("id", "strategy", "params", "sl", "tp", "reason") if key in selected},
        "train_top": sorted(train_rows, key=lambda row: (row["max_drawdown"], -row["final_equity"]))[:8],
        "baseline_spot_80bp": _public(baseline),
        "variants": variants,
        "recommendation": recommended,
        "slots": slots,
        "aggregate": aggregate,
        "limits": {symbol: {"ordermin": info["ordermin"], "costmin": info["costmin"]} for symbol, info in limits.items()},
        "futures_decimals": {symbol: futures[symbol]["extra"]["lot_decimals"] for symbol in futures},
    }


def _aggregate(rows: list[dict]) -> tuple[list[dict], str | None]:
    curves = []
    for row in rows:
        points = {}
        for item in row["equity"]:
            points[item["time"][:10]] = item["equity"]
        curves.append(points)
    days = sorted(set().union(*[set(points) for points in curves])) if curves else []
    last = [10.0] * len(curves)
    out = []
    peak = 10.0 * len(curves)
    halt = None
    for day in days:
        total = 0.0
        for i, points in enumerate(curves):
            if day in points:
                last[i] = points[day]
            total += last[i]
        peak = max(peak, total)
        drawdown = (peak - total) / peak if peak else 0.0
        if halt is None and drawdown + 1e-15 >= GLOBAL_DD:
            halt = day
        out.append({"time": day + "T00:00:00+00:00", "equity": total, "drawdown": drawdown})
    return out, halt


def render_markdown(payload: dict, written: str) -> str:
    lines = [
        "# 每筆 10 美元、多空與槓桿的樣本外比較",
        "",
        f"Written: {written} KST",
        "",
        "每個槽的保證金上限 10 美元，名義上限 20 美元。現貨槓桿固定 1，所以現貨單筆名義就是 10。永續 2 倍時保證金仍是 10、名義可以到 20。兩道上限都可以改，預設就是這兩個數。止損必帶。追高濾網仍開著：做多不追離 20 日低點 30% 以上的價，做空不追離 20 日高點 30% 以下的價。",
        "",
        "實盤仍只下 Kraken 現貨，而且只在現貨 1 倍、單筆名義不超過上限時才送。空單和 2 倍沒有接上實盤。不假設這個帳戶或這個地區開得了保證金或期貨。",
        "",
        "Kraken 保證金資格寫在官方頁：多數已驗證、住在美國、英國、加拿大、澳洲以外的客戶可以用；這四個地區另有條件。美國零售戶走另一套現貨保證金。出處：<https://support.kraken.com/articles/4402532394260-client-eligibility-for-margin-trading-services->。展期費每 4 小時一筆，費率隨市況鎖定在下單當下，公開歷史裡沒有這條序列。這次回測沒有編一個展期費。",
        "",
        "空單與 2 倍用的是 Kraken 線性永續成交 K 線（PF_XBTUSD、PF_ETHUSD、PF_SOLUSD）和公開 `relativeFundingRate`，每小時一筆。資金費序列從 "
        + payload["windows"]["funding_first"][:10]
        + " 開始，更早的日子沒有用 0 填，所以槓桿比較只做這一段。出處：<https://docs.kraken.com/api-reference/historical-funding-rates/historical-funding-rates>。最小檔位的維持保證金是交易所工具檔上的 0.5%，初始保證金 1%（交易所因此允許遠高於 2 倍）。這次程式把槓桿硬卡在 2。",
        "",
        "手續費仍跑 40 bp 與 80 bp，另加滑點 5 bp。這是現貨費率的壓力測試，不是把期貨的量級費率冒充成這兩個數。40 bp 只當對照，不拿來重挑參數。",
        "",
        f"訓練窗 {payload['windows']['train'][0]} 至 {payload['windows']['train'][1]}，只在 BTC 現貨、80 bp、只做多上面挑。規則事先寫死：至少 3 筆來回、期末不低於 10、權益沒有跌破 0.5 美元，然後取回撤最小的。樣本外從 {payload['windows']['oos'][0]} 起，不再改參數。",
        "",
        f"訓練挑中的是 `{payload['selected_on_train']['id']}`。止損 {payload['selected_on_train']['sl']}，止盈 {payload['selected_on_train']['tp']}。",
        "",
        "## 現貨基準",
        "",
        "同一套凍結參數，BTC 現貨、80 bp、2025-11-11 至最新已收盤日。這是用來對照先前 RSI+MACD 預設參數那次 10 美元變成 11.46、回撤 6.5% 的窗口。下面是這次重算，不是把舊數字再抄一次。",
        "",
    ]
    base = payload["baseline_spot_80bp"]
    lines.append(
        f"期末 {base['final_equity']:.2f} 美元，報酬 {(base['total_return'] or 0) * 100:.1f}%，最大回撤 {base['max_drawdown'] * 100:.1f}%，來回 {base['round_trips']}，生存 {base['survival_days']} 天，最長連虧 {base['worst_losing_streak']}。"
    )
    lines.extend(["", "## 樣本外：只做多 / 多空、1 倍 / 2 倍", ""])
    lines.append("| 組 | 費率 | 期末 | 報酬 | 最大回撤 | 年化 | 天數 | 來回 | 資金費 | 生存天 | 跌破 0.5 | 連虧 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in payload["variants"]:
        cagr_text = "—" if row["cagr"] is None else f"{row['cagr'] * 100:.1f}%"
        lines.append(
            f"| {row['name']} | {row['fee_bps']:.0f} | {row['final_equity']:.2f} | {row['total_return'] * 100:.1f}% | {row['max_drawdown'] * 100:.1f}% | {cagr_text} | {row['span_days']} | {row['round_trips']} | {row['funding_paid']:.3f} | {row['survival_days']} | {'否' if row['survived'] else '是'} | {row['worst_losing_streak']} |"
        )
    lines.extend(["", "## 推薦", ""])
    rec = payload["recommendation"]
    lines.append(rec["reason_zh"])
    if rec.get("id"):
        chosen = next(row for row in payload["variants"] if row["id"] == rec["id"])
        lines.append("")
        lines.append(
            f"推薦 `{chosen['id']}`。期末 {chosen['final_equity']:.2f}，最大回撤 {chosen['max_drawdown'] * 100:.1f}%，來回 {chosen['round_trips']}。"
        )
        if chosen["cagr"] is not None:
            lines.append(
                f"這段 {chosen['span_days']} 天的年化是 {chosen['cagr'] * 100:.1f}%。樣本外不到一年，年化會把一段噪音放大，總報酬才是這段真正賺到的。"
            )
        lines.append("目標是又快又穩。回撤較小而且本金還在，才留在推薦裡。報酬更高但回撤更大的 2 倍或做空，不會因為訓練期好看就被換成推薦。")
    lines.extend(["", "### 各年片段", ""])
    if rec.get("id"):
        chosen = next(row for row in payload["variants"] if row["id"] == rec["id"])
        for item in chosen["year_slices"]:
            ret = "—" if item["return"] is None else f"{item['return'] * 100:.1f}%"
            lines.append(f"- {item['year']}：{item['bars']} 根，片段報酬 {ret}，期末 {item['end_equity']:.2f}。這不是完整日曆年。")
    agg = payload.get("aggregate") or {}
    lines.extend(["", "## 多槽", ""])
    if not agg:
        lines.append("沒有組成多槽。上面沒有可推薦的樣本外組。")
    else:
        lines.append(
            f"{agg['slot_count']} 個槽，每個保證金 10 美元，合計起始 {agg['capital']:.0f}。槽分在不同交易對，進場再錯開 0、5、10 根，避免全部是同一筆單。全域回撤熔斷 15%。"
        )
        halt = agg.get("global_halt") or "沒有觸發"
        lines.append(f"全域熔斷：{halt}。期末 {agg['final_equity']:.2f}，最大回撤 {agg['max_drawdown'] * 100:.1f}%。")
        if agg.get("cagr") is not None:
            lines.append(f"多槽這段的年化 {agg['cagr'] * 100:.1f}%。槽與槽如果其實同向，年化不會因為槽數變多就更穩。")
        lines.append("")
        lines.append("| 槽 | 延遲 | 期末 | 回撤 | 來回 | 生存天 | 跌破 0.5 | 連虧 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in payload["slots"]:
            lines.append(
                f"| {row['symbol']} | {row['delay']} | {row['final_equity']:.2f} | {row['max_drawdown'] * 100:.1f}% | {row['round_trips']} | {row['survival_days']} | {'否' if row['survived'] else '是'} | {row['worst_losing_streak']} |"
            )
    lines.extend(
        [
            "",
            "## 不要把這張表讀成成長保證",
            "",
            "格子是事先寫死的，但格子本身已經試了很多次。推薦只看樣本外，而且回撤優先。資金費只覆蓋公開序列有的日子。現貨保證金的展期費沒有歷史可扣，所以沒有算進永續那一欄，也沒有用一個固定費率代替。10 美元在 BTC 永續上會受到合約小數位限制，名義被上限卡住以後若湊不滿一檔，那一筆就不會出現。這不是投資建議。",
            "",
        ]
    )
    return "\n".join(lines)


def write_study(root: Path, payload: dict) -> tuple[Path, Path]:
    root = Path(root)
    json_path = root / "results" / "slot_book.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    stored = json.loads(json.dumps(payload, default=str))
    json_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    from zoneinfo import ZoneInfo

    kst = now_utc().astimezone(ZoneInfo("Asia/Seoul"))
    doc_path = root / "docs" / f"{kst.strftime('%m%d_%H%M')}_seoul_每單10u多槽優化.md"
    doc_path.write_text(render_markdown(payload, kst.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    return json_path, doc_path


def main() -> None:
    raise SystemExit("一個月高頻的樣本外在 python -m ymzz.hf。這支日線一年研究沒有執行。")


if __name__ == "__main__":
    main()
