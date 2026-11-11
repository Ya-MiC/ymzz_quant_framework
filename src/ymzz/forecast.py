"""用最新已收盤價描述 RSI+MACD 現在的位置。不下單。"""

from __future__ import annotations

import math

from ymzz.strategies import compute_signals


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def describe_forecast(closes: list[float], params: dict, *, qty: float = 0.0, entry: float | None = None, sl_pct: float = 0.05, tp_pct: float = 0.10) -> dict:
    signals, indicators, warnings = compute_signals("rsi_macd", closes, params, False)
    exits = indicators.get("_indicator_exit") or []
    rsi = _num((indicators.get("rsi") or [None])[-1])
    dif = _num((indicators.get("dif") or [None])[-1])
    dea = _num((indicators.get("dea") or [None])[-1])
    prev_dif = _num((indicators.get("dif") or [None, None])[-2]) if len(indicators.get("dif") or []) > 1 else None
    prev_dea = _num((indicators.get("dea") or [None, None])[-2]) if len(indicators.get("dea") or []) > 1 else None
    distance = None
    side = "指標還沒有齊"
    if dif is not None and dea is not None:
        distance = dif - dea
        if distance > 0:
            side = "DIF 在 DEA 之上（金叉的一側）"
        elif distance < 0:
            side = "DIF 在 DEA 之下（死叉的一側）"
        else:
            side = "DIF 與 DEA 重疊"
    signal = float(signals[-1] or 0) if signals else 0.0
    exit_flag = float(exits[-1] or 0) if exits else 0.0
    if signal > 0 and qty <= 0:
        nxt = "回測規則下，這個收盤訊號會在下一根開盤嘗試買入。還要過最小下單量與當日熔斷。"
    elif exit_flag > 0 and qty > 0:
        nxt = "回測規則下，指標出場會在這根之後處理。紙上 runner 是在看到這根已收盤時出場。"
    elif qty > 0:
        nxt = "目前視為持倉。下一根若先碰到止損或止盈，止損優先。"
    else:
        nxt = "這根收盤沒有新的買入脈衝。下一根開盤不會因為這一根而開倉。"
    levels = None
    if entry and qty > 0:
        levels = {"stop_loss": entry * (1.0 - sl_pct), "take_profit": entry * (1.0 + tp_pct), "entry": entry}
    return {
        "rsi14": rsi,
        "dif": dif,
        "dea": dea,
        "prev_dif": prev_dif,
        "prev_dea": prev_dea,
        "distance_dif_minus_dea": distance,
        "macd_side_zh": side,
        "distance_note_zh": "距離是 DIF 減 DEA，單位與價格相同，不是報酬率。正值表示已在金叉一側。",
        "next_candle_zh": nxt,
        "stop_take": levels,
        "warnings": warnings,
        "bars": len(closes),
    }
