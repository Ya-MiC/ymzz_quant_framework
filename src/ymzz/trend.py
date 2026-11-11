"""低頻趨勢。目標距離至少是來回做市費的 3 倍。執行時不重挑，也不送單。

候選在這個檔案裡先寫死。訓練只看 PF_XBTUSD 的 4 小時、最近 30 日之前。
樣本外才評全部預先登記的列。正的樣本外列不會反過來改寫凍結的那一組。
失敗過的策略留在倉庫裡，紀錄在 docs/failures/。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ymzz.data import FUTURES_DECIMALS, FUTURES_MARKET
from ymzz.engine import Bar
from ymzz.errors import ConfigError
from ymzz.hf import SPOT_MAKER_BPS, SPOT_TAKER_BPS, SLIP_BPS, _cfg_from, choose_hf, run_hf, trigger_signals
from ymzz.indicators import atr, sma

# 帳戶實測：做市 0.40%、吃單 0.80%。兩邊都掛限價時，來回至少 0.80%。
ROUND_TRIP_MAKER = (SPOT_MAKER_BPS + SPOT_MAKER_BPS) / 10000.0
MIN_MOVE = ROUND_TRIP_MAKER * 3.0
EXPIRE = 3
CHASE_RISE = 0.30
CHASE_DAYS = 20
RSI_LOW = 30.0
RSI_HIGH = 70.0
ATR_PERIOD = 14
ATR_MIN = 0.01
REGIME_SMA = 100
STOPS = ((0.012, 0.024), (0.024, 0.048))
SELECT_SYMBOL = "BTC/USD"
SELECT_TIMEFRAME = "4h"
BARS_PER_DAY = {"1h": 24, "4h": 6}

INDICATORS = {
    "donchian": ["donchian"],
    "donchian_regime": ["donchian", "sma", "atr"],
    "vol_trend": ["sma", "atr"],
    "rsi": ["rsi"],
    "rsi_regime": ["rsi", "sma", "atr"],
}


def _spec(**kwargs) -> dict:
    if kwargs["tp"] + 1e-12 < MIN_MOVE:
        raise ConfigError("目標距離必須至少是來回做市費的 3 倍")
    if kwargs["sl"] >= kwargs["tp"]:
        raise ConfigError("止損必須小於止盈")
    family = kwargs["family"]
    if family not in INDICATORS or len(INDICATORS[family]) > 3:
        raise ConfigError("決策指標超過 3 個")
    return kwargs


def _grid() -> list[dict]:
    rows = []
    for lookback in (20, 55):
        for sl, tp in STOPS:
            for side in ("long", "both"):
                rows.append(_spec(family="donchian", lookback=lookback, sl=sl, tp=tp, side=side, trend=REGIME_SMA))
                rows.append(_spec(family="donchian_regime", lookback=lookback, sl=sl, tp=tp, side=side, trend=REGIME_SMA))
    for trend in (50, 100):
        for sl, tp in STOPS:
            for side in ("long", "both"):
                rows.append(_spec(family="vol_trend", trend=trend, sl=sl, tp=tp, side=side))
    for period in (12, 14, 22):
        for sl, tp in STOPS:
            for side in ("long", "both"):
                rows.append(_spec(family="rsi", period=period, sl=sl, tp=tp, side=side, trend=REGIME_SMA))
                rows.append(_spec(family="rsi_regime", period=period, sl=sl, tp=tp, side=side, trend=REGIME_SMA))
    return rows


GRID = _grid()


def spec_id(spec: dict) -> str:
    if spec["family"] in {"donchian", "donchian_regime"}:
        knob = f"n{spec['lookback']}"
    elif spec["family"] == "vol_trend":
        knob = f"sma{spec['trend']}"
    else:
        knob = f"rsi{spec['period']}"
    return f"{spec['family']}|{knob}|sl{spec['sl']}|tp{spec['tp']}|{spec['side']}"


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def build_signals(bars: list[Bar], spec: dict, chase_bars: int) -> list[float]:
    """收盤才知道。通道、均線、ATR、RSI 都不用當根本身以後的價格。"""
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    family = spec["family"]
    out = [0.0] * len(bars)
    pulses = trigger_signals(closes, int(spec["period"]), RSI_LOW, RSI_HIGH) if family in {"rsi", "rsi_regime"} else None
    trend = sma(closes, int(spec["trend"])) if family in {"donchian_regime", "rsi_regime", "vol_trend"} else None
    width = atr(highs, lows, closes, ATR_PERIOD) if family in {"donchian_regime", "rsi_regime", "vol_trend"} else None
    lookback = int(spec.get("lookback") or 0)
    for i in range(len(bars)):
        signal = 0.0
        if family in {"donchian", "donchian_regime"}:
            if i < lookback + 1:
                continue
            upper = max(highs[i - lookback : i])
            lower = min(lows[i - lookback : i])
            prev_upper = max(highs[i - 1 - lookback : i - 1])
            prev_lower = min(lows[i - 1 - lookback : i - 1])
            if closes[i] <= 0 or (upper - lower) / closes[i] < MIN_MOVE:
                continue
            if closes[i - 1] <= prev_upper and closes[i] > upper:
                signal = 1.0
            elif spec["side"] == "both" and closes[i - 1] >= prev_lower and closes[i] < lower:
                signal = -1.0
        elif family == "vol_trend":
            if trend is None or width is None or i < 1 or not _finite(trend[i]) or not _finite(trend[i - 1]):
                continue
            if not _finite(width[i]) or closes[i] <= 0 or float(width[i]) / closes[i] < ATR_MIN:
                continue
            if closes[i - 1] <= float(trend[i - 1]) and closes[i] > float(trend[i]):
                signal = 1.0
            elif spec["side"] == "both" and closes[i - 1] >= float(trend[i - 1]) and closes[i] < float(trend[i]):
                signal = -1.0
        else:
            signal = float(pulses[i]) if pulses is not None else 0.0
            if spec["side"] == "long" and signal < 0:
                signal = 0.0
        if signal == 0.0:
            continue
        if family in {"donchian_regime", "rsi_regime"}:
            if trend is None or width is None or not _finite(trend[i]) or not _finite(width[i]) or closes[i] <= 0:
                continue
            if float(width[i]) / closes[i] < ATR_MIN:
                continue
            if signal > 0 and closes[i] <= float(trend[i]):
                continue
            if signal < 0 and closes[i] >= float(trend[i]):
                continue
        if chase_bars and i >= chase_bars:
            if signal > 0:
                floor = min(lows[i - chase_bars : i])
                if floor > 0 and closes[i] > floor * (1.0 + CHASE_RISE):
                    continue
            else:
                ceiling = max(highs[i - chase_bars : i])
                if ceiling > 0 and closes[i] < ceiling * (1.0 - CHASE_RISE):
                    continue
        out[i] = signal
    return out


def _split(bars: list[Bar], days: int = 30) -> int:
    cutoff = bars[-1].open_time - timedelta(days=days)
    count = 0
    for bar in bars:
        if bar.open_time < cutoff:
            count += 1
        else:
            break
    if count < 30 or count >= len(bars):
        raise ConfigError("這段 K 線切不出訓練窗和最近 30 日。沒有把同一段兩邊都用。")
    return count


def _window(bars: list[Bar]) -> dict:
    span = (bars[-1].open_time - bars[0].open_time).total_seconds() / 86400.0
    return {
        "n": len(bars),
        "first": bars[0].open_time.date().isoformat(),
        "last": bars[-1].open_time.date().isoformat(),
        "days": round(span, 2),
    }


def _metrics(result: dict) -> dict:
    keys = (
        "final_equity",
        "min_equity",
        "total_return",
        "max_drawdown",
        "round_trips",
        "win_rate",
        "fees_paid",
        "fees_funding",
        "missing_funding",
        "entries",
        "missed_limits",
        "skipped_lot",
        "max_entry_notional",
        "global_halt",
        "capital",
    )
    return {key: result[key] for key in keys}


def _run(bars, signals, spec, *, leverage=1.0, funding=None, decimals=4):
    notional = 10.0 * leverage
    result = run_hf(
        bars,
        signals,
        _cfg_from(
            {"sl": spec["sl"], "tp": spec["tp"], "expire": EXPIRE},
            leverage=leverage,
            max_margin_usd=10.0,
            max_notional_usd=notional,
            entry="maker",
            maker_bps=SPOT_MAKER_BPS,
            taker_bps=SPOT_TAKER_BPS,
            slip_bps=SLIP_BPS,
            lot_decimals=decimals,
            store_equity=False,
        ),
        funding,
    )
    if result["max_entry_notional"] > notional + 0.05:
        raise ConfigError(f"進場名義 {result['max_entry_notional']} 超過 {notional}")
    return result


def _row(spec, symbol, timeframe, bars, result, kind) -> dict:
    return {
        "id": f"{symbol}|{timeframe}|{spec_id(spec)}",
        "symbol": symbol,
        "market": FUTURES_MARKET[symbol],
        "timeframe": timeframe,
        "family": spec["family"],
        "indicators": list(INDICATORS[spec["family"]]),
        "lookback": spec.get("lookback"),
        "period": spec.get("period"),
        "trend": spec.get("trend") if spec["family"] in {"vol_trend", "donchian_regime", "rsi_regime"} else None,
        "sl": spec["sl"],
        "tp": spec["tp"],
        "side": spec["side"],
        "kind": kind,
        "fee_label": "做市40/吃單80 bp",
        **_window(bars),
        **_metrics(result),
    }


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _history_md() -> str:
    return "\n".join(
        [
            "## 已失敗、留在倉庫裡的版本",
            "",
            "這些策略沒有刪。程式、測試和舊報告都還在。",
            "",
            "### RSI+MACD 日線，公開現貨約兩年",
            "",
            "Kraken 現貨 XBTUSD 日線，2024-11-21 至 2026-11-10，720 根。這是公開 OHLC 實際給得出的長度，大約 715 日。",
            "規則是倉庫裡的 `rsi_macd`：RSI(14) 在 40 到 70 且 MACD(12,26,9) 金叉才做多，止損 5%，止盈 10%，本金 10 美元，現貨 1 倍。",
            "吃單 80 bp、滑點 5 bp：期末 7.0615 美元，報酬 -29.39%，最大回撤 41.62%，21 筆，勝率 23.8%，手續費 2.5365。這就是先前記的約 -29.4%／回撤 41.6%。",
            "同一段改成做市 40 bp 仍是虧的：報酬 -16.47%，回撤 34.18%，勝率 28.6%。費用會把虧損加深，但不是唯一原因。",
            "同一套 80 bp 規則只看最後 366 日（2025-11-10 至 2026-11-10）是報酬 +14.88%、回撤 5.92%、6 筆、勝率 50%。先前掃描裡 2025-11-11 起的 365 根是 +14.6%、回撤 6.5%。一年看起來能賺錢，拉長到公開日線的全長就虧。那一年不是可以外推的樣本。",
            "",
            "### 高頻 RSI(14) 30/70，1 小時",
            "",
            "規則留在 `ymzz.hf`，報告在 `docs/1111_1608_seoul_一個月高頻觸發.md`。訓練沒有任何一組守住 10 美元。凍結的是 RSI(14) 上穿 30、下穿 70，止損 0.8%，止盈 2.5%。",
            "最近 30 日、現貨費率：限價進場 -4.82%（回撤 4.82%，3 筆，勝率 0%，手續費 0.2819）；市價進場 -14.88%（回撤 15.48%，9 筆，手續費 1.1432）；2 倍 -9.62%（回撤 9.62%，手續費 0.5639）。",
            "現貨 1 小時公開 K 線只有約 30 日，不能再切一塊更早的訓練。永續 1 小時當時只用到約 83 日。",
            "",
            "### 為什麼失敗",
            "",
            "- 帳戶 30 日成交量是 0，費率就是做市 0.40%、吃單 0.80%。兩邊都做市，來回至少 0.80%。止損若用吃單，再加 5 bp 滑點，單次出場成本更高。高頻的止盈只有 0.8% 到 2.5%，利潤空間被費吃掉。",
            "- 日線 RSI+MACD 在最近一年是正的，在公開日線的全長是 -29.39%。用一年的窗口挑規則，會把更早的虧損留在樣本外面。",
            "- 高頻那組樣本外勝率是 0。日線兩年的勝率是 23.8%。勝率低，又沒有遠大於費用的單筆空間。",
            "- 現貨 1 分鐘到 1 小時的公開歷史太短，做不到「訓練完再留 30 日」。短樣本上的正報酬不能當成 30 日樣本外。",
            "",
            "### 因此這次改了什麼",
            "",
            "- 目標距離只允許 2.4% 或 4.8%。2.4% 剛好是來回做市費 0.80% 的 3 倍。更小的目標不再進候選。",
            "- 家族是唐奇安突破、波動濾網下的趨勢，以及 RSI 穿越。RSI 週期預先寫上 12、14、22，穿越版和趨勢濾網版都有。",
            "- 沒有趨勢加波動的狀態就保持空手。不追漲：收盤比前 20 日低點高出 30% 就不做多，空單對稱。",
            "- 仍是每筆名義最多 10 美元（2 倍最多 20），進場就附止損和止盈，槓桿不超過 2。2 倍只評凍結的那一組，不拿來重挑。",
            "",
        ]
    )


def render_report(payload: dict, written: str) -> str:
    frozen = payload["frozen"]
    verdict = payload["verdict_zh"]
    lines = [
        "# 低頻趨勢，費後驗證",
        "",
        f"Written: {written} KST",
        "",
        verdict,
        "",
        "沒有送出實盤。紙上的高頻預設也沒有換成這次樣本外較好看的任何一列。",
        "",
        "## 費用門檻",
        "",
        "帳戶在 XBTUSDC 的 30 日成交量是 0，費率是做市 0.40%、吃單 0.80%。兩邊都是限價時，來回 0.80%。這次要求單筆目標至少 2.40%，也就是 3 倍。",
        "進場只掛限價，做市 40 bp。止盈也是限價。止損是吃單 80 bp，再加 5 bp 滑點。所以真的被打止損時，成本高於 0.80%。樣本外的報酬用的是這套實際扣費，不是用 3 倍這個門檻本身代替結果。",
        "",
        "## 事先寫死的候選",
        "",
        f"共 {len(GRID)} 組。決策指標都不超過 3 個。止損／止盈只有 (1.2%, 2.4%) 和 (2.4%, 4.8%)。限價留 {EXPIRE} 根。",
        "唐奇安 20 與 55：收盤突破前一根通道才進場，而且通道寬度至少 2.4%，否則跳過。",
        f"趨勢加波動：收盤在 SMA 的同一側，而且 ATR({ATR_PERIOD})/收盤至少 {ATR_MIN:.0%}。不滿足就保持空手。濾網用在唐奇安和 RSI 上。另有一組只靠均線穿越加這個波動門檻。",
        "RSI 穿越的週期是 12、14、22，超賣 30、超買 70。14 留在裡面，是為了和 12、22 並排，不是事後補上去的。",
        "多單、以及多空都做，兩種都在候選裡。",
        f"挑選只看 {SELECT_SYMBOL} {SELECT_TIMEFRAME} 的訓練窗：來回至少 5 筆、權益不低於 0.5、期末至少回到 10 美元，然後取回撤最小。沒有守住 10 美元，就凍結期末最高且仍有 5 筆的一組，並寫明訓練已虧。其他商品和 1 小時只做轉移，不重挑。",
        "",
        "## 資料長度",
        "",
        "| 市場 | 週期 | 根數 | 起 | 迄 | 天 | 訓練根數 | 缺口 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["coverage"]:
        lines.append(
            f"| {row['market']} | {row['timeframe']} | {row['n']} | {row['first']} | {row['last']} | {row['days']} | {row['train_n']} | {row['gaps']} |"
        )
    lines.extend(
        [
            "",
            "價格是 Kraken 線性永續的成交 K 線，不是現貨。費用用現貨這一級。1 倍不扣資金費，因為這一列對應的是現貨成本。2 倍那一列才扣真實的 relativeFundingRate，缺的小時沒有填 0。",
            "",
            "## 訓練窗凍結的一組",
            "",
            f"標記 `{payload['selection_tag']}`。{frozen['id']}。",
            f"訓練期末 {payload['train']['final_equity']:.4f}，報酬 {_pct(payload['train']['total_return'])}，回撤 {_pct(payload['train']['max_drawdown'])}，來回 {payload['train']['round_trips']}，勝率 {_pct(payload['train']['win_rate'])}，手續費 {payload['train']['fees_paid']:.4f}。",
            "",
            "這組在各市場最近 30 日的結果。2 倍是同一組的附列，沒有參與挑選。",
            "",
            "| 市場 | 週期 | 倍數 | 報酬 | 回撤 | 來回 | 勝率 | 手續費 | 資金費 | 期末 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["frozen_oos"]:
        lines.append(
            f"| {row['market']} | {row['timeframe']} | {row.get('leverage', 1)} | {_pct(row['total_return'])} | {_pct(row['max_drawdown'])} | "
            f"{row['round_trips']} | {_pct(row['win_rate'])} | {row['fees_paid']:.4f} | {row['fees_funding']:.4f} | {row['final_equity']:.4f} |"
        )
    lines.extend(["", "## RSI 12、14、22 的 30 日樣本外", ""])
    lines.append("每一列都是預先登記的。報酬大於 0 也不會改成上面凍結的那組。")
    lines.append("")
    lines.append("| 列 | RSI | 濾網 | 方向 | 止盈 | 市場 | 圖表 | 報酬 | 回撤 | 來回 | 勝率 | 手續費 | 期末 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in payload["rows"]:
        if row["family"] not in {"rsi", "rsi_regime"}:
            continue
        lines.append(
            f"| {row['id']} | {row['period']} | {'趨勢+波動' if row['family']=='rsi_regime' else '無'} | {row['side']} | {_pct(row['tp'])} | "
            f"{row['market']} | {row['timeframe']} | {_pct(row['total_return'])} | {_pct(row['max_drawdown'])} | {row['round_trips']} | "
            f"{_pct(row['win_rate'])} | {row['fees_paid']:.4f} | {row['final_equity']:.4f} |"
        )
    lines.extend(["", "## 其他家族有沒有樣本外大於 0", ""])
    lines.append("| 家族 | 樣本外列數 | 報酬大於 0 | 其中來回至少 5 筆 |")
    lines.append("| --- | --- | --- | --- |")
    for row in payload["family_counts"]:
        lines.append(f"| {row['family']} | {row['rows']} | {row['positive']} | {row['positive_enough']} |")
    if payload["positives"]:
        lines.extend(["", "報酬大於 0 的列記在下面。它們是事後數出來的，不是訓練窗挑的。", ""])
        lines.append("| 列 | 報酬 | 回撤 | 來回 | 勝率 | 手續費 | 期末 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in payload["positives"]:
            lines.append(
                f"| {row['id']} | {_pct(row['total_return'])} | {_pct(row['max_drawdown'])} | {row['round_trips']} | "
                f"{_pct(row['win_rate'])} | {row['fees_paid']:.4f} | {row['final_equity']:.4f} |"
            )
    else:
        lines.extend(["", "預先登記的 1 倍列裡，沒有任何一列在最近 30 日的費後報酬大於 0。", ""])
    lines.extend(
        [
            "## 失敗紀錄",
            "",
            "舊的失敗沒有刪，寫在 `docs/failures/`。這次若仍然沒有一列大於 0，也會補進同一份紀錄。",
            "",
        ]
    )
    return "\n".join(lines)


def render_failures(payload: dict, written: str) -> str:
    frozen = next(
        row
        for row in payload["frozen_oos"]
        if row["symbol"] == SELECT_SYMBOL and row["timeframe"] == SELECT_TIMEFRAME and row["leverage"] == 1.0
    )
    extra = []
    if payload["verdict_positive"] == 0:
        extra = [
            "### 低頻趨勢這一次",
            "",
            f"寫作時間同一輪。預先登記 {payload['n_rows']} 列 1 倍樣本外，報酬大於 0 的是 0 列。",
            f"訓練窗凍結的 `{payload['frozen']['id']}` 在 {frozen['market']} {frozen['timeframe']} 最近 30 日是報酬 {_pct(frozen['total_return'])}，回撤 {_pct(frozen['max_drawdown'])}，來回 {frozen['round_trips']}，手續費 {frozen['fees_paid']:.4f}。",
            "目標拉到費用的 3 倍之後，這一批仍然沒有在樣本外賺回手續費。規則留在 `ymzz.trend`，沒有刪。",
            "",
        ]
    else:
        extra = [
            "### 低頻趨勢這一次",
            "",
            f"預先登記的 1 倍樣本外裡，有 {payload['verdict_positive']} 列報酬大於 0。多數只有一兩筆。凍結的那組沒有改成其中最好的一列。細節在趨勢報告。",
            "",
        ]
    return "\n".join(
        [
            "# 失敗紀錄",
            "",
            f"Written: {written} KST",
            "",
            "這裡只記已經跑過、而且不該被刪掉的版本。數字來自這次倉庫裡的重跑或已存檔的結果，沒有另編一組。",
            "",
            _history_md(),
            *extra,
        ]
    )


def run_study(root: Path) -> dict:
    from ymzz.data import load_kraken_futures_history
    from ymzz.http_cache import DataClient
    from ymzz.timeutil import now_utc

    now = now_utc()
    client = DataClient(root / "data" / "cache")
    earliest = {
        "4h": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "1h": datetime(2026, 2, 1, tzinfo=timezone.utc),
    }
    series = {}
    coverage = []
    for symbol in ("BTC/USD", "ETH/USD", "SOL/USD"):
        for timeframe in ("4h", "1h"):
            bars, funding, meta = load_kraken_futures_history(client, symbol, timeframe, now, earliest[timeframe])
            bars = sorted(bars, key=lambda bar: bar.open_time)
            train_n = _split(bars, 30)
            series[(symbol, timeframe)] = {
                "bars": bars,
                "funding": funding,
                "train_n": train_n,
                "decimals": FUTURES_DECIMALS[FUTURES_MARKET[symbol]],
                "meta": meta,
            }
            coverage.append(
                {
                    "symbol": symbol,
                    "market": FUTURES_MARKET[symbol],
                    "timeframe": timeframe,
                    "train_n": train_n,
                    "gaps": len(meta.get("gaps") or []),
                    **_window(bars),
                }
            )
    select = series[(SELECT_SYMBOL, SELECT_TIMEFRAME)]
    train_bars = select["bars"][: select["train_n"]]
    chase = CHASE_DAYS * BARS_PER_DAY[SELECT_TIMEFRAME]
    train_rows = []
    for spec in GRID:
        signals = build_signals(train_bars, spec, chase)
        result = _run(train_bars, signals, spec, decimals=select["decimals"])
        train_rows.append({"id": spec_id(spec), "spec": spec, "capital": 10.0, **_metrics(result)})
    chosen_row, tag = choose_hf(train_rows)
    chosen = chosen_row["spec"]
    # 參數在這裡凍結。下面才跑樣本外。
    oos_rows = []
    for (symbol, timeframe), pack in series.items():
        bars = pack["bars"]
        train_n = pack["train_n"]
        chase_bars = CHASE_DAYS * BARS_PER_DAY[timeframe]
        oos_bars = bars[train_n:]
        for spec in GRID:
            signals = build_signals(bars, spec, chase_bars)[train_n:]
            result = _run(oos_bars, signals, spec, decimals=pack["decimals"])
            oos_rows.append(_row(spec, symbol, timeframe, oos_bars, result, "oos"))
    frozen_oos = []
    for (symbol, timeframe), pack in series.items():
        bars = pack["bars"]
        train_n = pack["train_n"]
        oos_bars = bars[train_n:]
        signals = build_signals(bars, chosen, CHASE_DAYS * BARS_PER_DAY[timeframe])[train_n:]
        for leverage, funding in ((1.0, None), (2.0, pack["funding"])):
            result = _run(oos_bars, signals, chosen, leverage=leverage, funding=funding, decimals=pack["decimals"])
            row = _row(chosen, symbol, timeframe, oos_bars, result, "frozen")
            row["leverage"] = leverage
            row["id"] = row["id"] + f"|{leverage:.0f}x"
            frozen_oos.append(row)
    positives = [
        row
        for row in oos_rows
        if row["total_return"] is not None and row["total_return"] > 0 and row["round_trips"] > 0
    ]
    positives.sort(key=lambda row: (-row["total_return"], row["max_drawdown"]))
    family_counts = []
    for family in INDICATORS:
        group = [row for row in oos_rows if row["family"] == family]
        family_counts.append(
            {
                "family": family,
                "rows": len(group),
                "positive": sum(1 for row in group if row["total_return"] is not None and row["total_return"] > 0 and row["round_trips"] > 0),
                "positive_enough": sum(
                    1
                    for row in group
                    if row["total_return"] is not None and row["total_return"] > 0 and row["round_trips"] >= 5
                ),
            }
        )
    headline = next(row for row in frozen_oos if row["symbol"] == SELECT_SYMBOL and row["timeframe"] == SELECT_TIMEFRAME and row["leverage"] == 1.0)
    if not positives:
        verdict = (
            f"沒有。預先登記的 {len(oos_rows)} 列 1 倍規則裡，沒有任何一列在最近 30 日費後報酬大於 0。"
            f"訓練窗凍結的那組在 {headline['market']} {headline['timeframe']} 是 {_pct(headline['total_return'])}。"
        )
    else:
        enough = sum(1 for row in positives if row["round_trips"] >= 5)
        verdict = (
            f"訓練窗凍結的規則沒有在樣本外賺到錢。{headline['market']} {headline['timeframe']} 最近 30 日是 {_pct(headline['total_return'])}，"
            f"已平倉 {headline['round_trips']} 筆。"
            f"{len(oos_rows)} 列裡有 {len(positives)} 列帳面報酬大於 0，其中 {len(positives) - enough} 列只有 1 或 2 筆。"
            f"來回至少 5 筆且大於 0 的有 {enough} 列。這些正數不是訓練窗挑的，沒有改成要跑的規則。"
        )
    return {
        "selection_tag": tag,
        "frozen": {
            "id": spec_id(chosen),
            "family": chosen["family"],
            "lookback": chosen.get("lookback"),
            "period": chosen.get("period"),
            "trend": chosen.get("trend"),
            "sl": chosen["sl"],
            "tp": chosen["tp"],
            "side": chosen["side"],
            "indicators": list(INDICATORS[chosen["family"]]),
        },
        "train": {key: chosen_row[key] for key in chosen_row if key != "spec"},
        "train_grid": [{key: row[key] for key in row if key != "spec"} for row in train_rows],
        "coverage": coverage,
        "rows": oos_rows,
        "frozen_oos": frozen_oos,
        "positives": positives,
        "family_counts": family_counts,
        "verdict_zh": verdict,
        "verdict_positive": len(positives),
        "n_rows": len(oos_rows),
        "min_move": MIN_MOVE,
        "round_trip_maker": ROUND_TRIP_MAKER,
    }


def write_study(root: Path, payload: dict) -> tuple[Path, Path, Path]:
    import json

    from ymzz.timeutil import now_utc, to_kst

    moment = to_kst(now_utc())
    written = moment.strftime("%Y-%m-%d %H:%M")
    stamp = moment.strftime("%m%d_%H%M")
    slim = []
    for row in payload["rows"]:
        slim.append(row)
    stored = {**payload, "rows": slim, "positives": payload["positives"]}
    out = root / "results"
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "trend_oos.json"
    json_path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    doc = root / "docs" / f"{stamp}_seoul_低頻趨勢費後驗證.md"
    doc.write_text(render_report(payload, written), encoding="utf-8")
    fail_dir = root / "docs" / "failures"
    fail_dir.mkdir(parents=True, exist_ok=True)
    fail = fail_dir / f"{stamp}_seoul_失敗紀錄.md"
    fail.write_text(render_failures(payload, written), encoding="utf-8")
    return json_path, doc, fail


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    payload = run_study(root)
    json_path, doc, fail = write_study(root, payload)
    print(json_path)
    print(doc)
    print(fail)
    print(payload["selection_tag"], payload["frozen"]["id"])
    print(payload["verdict_zh"])


if __name__ == "__main__":
    main()
