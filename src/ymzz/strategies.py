"""策略外掛。每條策略最多三個決策指標，訊號在收盤確定。"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ymzz.errors import ConfigError
from ymzz.indicators import bollinger, ema, macd, rsi, sma


def _finite_pair(a, b, i: int) -> bool:
    return np.isfinite(a[i]) and np.isfinite(b[i]) and np.isfinite(a[i - 1]) and np.isfinite(b[i - 1])


def state_cross(fast, slow, allow_short: bool = False) -> np.ndarray:
    """快線上穿慢線做多；下穿則平倉，allow_short 時改做空。"""
    fast = np.asarray(fast, dtype=float)
    slow = np.asarray(slow, dtype=float)
    sig = np.zeros(len(fast))
    pos = 0.0
    for i in range(1, len(fast)):
        if not _finite_pair(fast, slow, i):
            sig[i] = pos
            continue
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            pos = 1.0
        elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            pos = -1.0 if allow_short else 0.0
        sig[i] = pos
    return sig


def rsi_threshold_signal(values, oversold: float, overbought: float, allow_short: bool = False) -> np.ndarray:
    """RSI 從下方上穿超賣線做多；從上方下穿超買線平倉。觸發線就是這一條 RSI。"""
    r = np.asarray(values, dtype=float)
    sig = np.zeros(len(r))
    pos = 0.0
    for i in range(1, len(r)):
        if not (np.isfinite(r[i]) and np.isfinite(r[i - 1])):
            sig[i] = pos
            continue
        if r[i - 1] < oversold and r[i] >= oversold:
            pos = 1.0
        elif r[i - 1] > overbought and r[i] <= overbought:
            pos = -1.0 if allow_short else 0.0
        sig[i] = pos
    return sig


def rsi_three_signal(short, mid, long, allow_short: bool = False) -> np.ndarray:
    """三線 RSI。

    觸發線是「短週期」那一條，不是自動挑最小數字。
    金叉：短線由下向上穿越中線，而且當根短線 > 長線，才做多。
    短線上穿中線但短線沒有站上長線時，忽略這次金叉，倉位不變。
    死叉：短線由上向下穿越中線，無條件平倉；allow_short 時改做空，不再看長線。
    """
    short = np.asarray(short, dtype=float)
    mid = np.asarray(mid, dtype=float)
    long = np.asarray(long, dtype=float)
    sig = np.zeros(len(short))
    pos = 0.0
    for i in range(1, len(short)):
        if not (
            np.isfinite(short[i])
            and np.isfinite(mid[i])
            and np.isfinite(long[i])
            and np.isfinite(short[i - 1])
            and np.isfinite(mid[i - 1])
        ):
            sig[i] = pos
            continue
        cross_up = short[i - 1] <= mid[i - 1] and short[i] > mid[i]
        cross_down = short[i - 1] >= mid[i - 1] and short[i] < mid[i]
        if cross_up and short[i] > long[i]:
            pos = 1.0
        elif cross_down:
            pos = -1.0 if allow_short else 0.0
        sig[i] = pos
    return sig


def bollinger_reversion_signal(close, lower, mid) -> np.ndarray:
    """收盤由上跌破下軌做多；之後由下站回中軌平倉。上軌只畫出來，不參與決策。"""
    close = np.asarray(close, dtype=float)
    lower = np.asarray(lower, dtype=float)
    mid = np.asarray(mid, dtype=float)
    sig = np.zeros(len(close))
    pos = 0.0
    for i in range(1, len(close)):
        if not (
            np.isfinite(close[i])
            and np.isfinite(lower[i])
            and np.isfinite(mid[i])
            and np.isfinite(close[i - 1])
            and np.isfinite(lower[i - 1])
            and np.isfinite(mid[i - 1])
        ):
            sig[i] = pos
            continue
        if close[i - 1] >= lower[i - 1] and close[i] < lower[i]:
            pos = 1.0
        elif pos > 0 and close[i - 1] <= mid[i - 1] and close[i] > mid[i]:
            pos = 0.0
        sig[i] = pos
    return sig


def grid_exposure(close, lower: float, upper: float, n_grids: int) -> np.ndarray:
    """等差網格的目標曝險，範圍 0 到 1。

    格線含上下邊界，共 n_grids+1 條。曝險 = min(格數, 嚴格高於收盤價的格線數) / 格數。
    價格在兩格之間不會改變曝險。下邊界以下為滿倉，上邊界以上為空倉。
    這只用價格相對格線，沒有看未來高低點。
    """
    if n_grids < 2 or n_grids > 20:
        raise ConfigError("網格數須在 2 到 20")
    if not (lower < upper):
        raise ConfigError("網格下界必須低於上界")
    close = np.asarray(close, dtype=float)
    lines = np.linspace(lower, upper, n_grids + 1)
    out = np.zeros(len(close))
    for i, price in enumerate(close):
        if not np.isfinite(price):
            out[i] = 0.0
            continue
        units = min(n_grids, int(np.sum(lines > price)))
        out[i] = units / n_grids
    return out


@dataclass
class ParamSpec:
    key: str
    label_zh: str
    type: str
    default: float
    min: float | None = None
    max: float | None = None
    help_zh: str = ""


@dataclass
class StrategySpec:
    id: str
    name_zh: str
    summary_zh: str
    formula_zh: str
    source_zh: str
    decision_indicators: list[str]
    display_indicators: list[str]
    params: list[ParamSpec] = field(default_factory=list)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name_zh": self.name_zh,
            "summary_zh": self.summary_zh,
            "formula_zh": self.formula_zh,
            "source_zh": self.source_zh,
            "decision_indicators": list(self.decision_indicators),
            "display_indicators": list(self.display_indicators),
            "params": [p.__dict__ for p in self.params],
        }


def _p(key, label, default, min_=None, max_=None, help_zh="", type_="number") -> ParamSpec:
    return ParamSpec(key, label, type_, default, min_, max_, help_zh)


STRATEGIES: dict[str, StrategySpec] = {
    "rsi_single": StrategySpec(
        id="rsi_single",
        name_zh="RSI 單線",
        summary_zh="一條 RSI。上穿超賣線做多，下穿超買線平倉。",
        formula_zh="觸發線就是這條 RSI。預設週期 14、超賣 30、超買 70。訊號在收盤確認，下一根開盤才成交。",
        source_zh="Wilder（1978）平滑 RSI。本倉庫自行計算，沒有複製 pandas-ta 或 freqtrade。",
        decision_indicators=["rsi"],
        display_indicators=["rsi"],
        params=[
            _p("period", "RSI 週期", 14, 2, 200, "決策只用這一條。"),
            _p("oversold", "超賣線", 30, 1, 49),
            _p("overbought", "超買線", 70, 51, 99),
        ],
    ),
    "rsi_three": StrategySpec(
        id="rsi_three",
        name_zh="RSI 三線",
        summary_zh="短、中、長三條 RSI。短線穿越中線才觸發，長線當濾網。",
        formula_zh=(
            "預設週期 6／12／24，三個欄位都可改，例如短 14、中 19、長 24。"
            "觸發線永遠是「短週期」欄位：金叉＝短線上穿中線，且當根短線高於長線才做多；"
            "死叉＝短線下穿中線就平倉。程式不會把三個數字重新排序。"
        ),
        source_zh="三條 Wilder RSI 的交叉規則由本框架定義並寫明，不是某個 GPL 策略檔的拷貝。",
        decision_indicators=["rsi_short", "rsi_mid", "rsi_long"],
        display_indicators=["rsi_short", "rsi_mid", "rsi_long"],
        params=[
            _p("rsi_short", "短週期（觸發線）", 6, 2, 200, "上穿或下穿中線的是這一條。"),
            _p("rsi_mid", "中週期（被穿越）", 12, 2, 300),
            _p("rsi_long", "長週期（濾網）", 24, 2, 400, "金叉時短線必須高於這一條。"),
        ],
    ),
    "macd": StrategySpec(
        id="macd",
        name_zh="MACD 交叉",
        summary_zh="DIF 上穿 DEA 做多，下穿平倉。",
        formula_zh="DIF = EMA(快) − EMA(慢)，DEA = EMA(DIF, 訊號)。柱狀圖 = DIF − DEA，只顯示，不單獨當決策。預設 12／26／9。",
        source_zh="Gerald Appel 的 MACD。EMA 以 SMA 起算。實作為本倉庫原創。",
        decision_indicators=["dif", "dea"],
        display_indicators=["dif", "dea", "macd_hist"],
        params=[
            _p("fast", "快線", 12, 2, 100),
            _p("slow", "慢線", 26, 3, 300),
            _p("signal", "訊號線", 9, 2, 100),
        ],
    ),
    "bollinger": StrategySpec(
        id="bollinger",
        name_zh="布林帶回歸",
        summary_zh="跌破下軌做多，站回中軌平倉。",
        formula_zh="中軌 = SMA(n)，上下軌 = 中軌 ± k × 母體標準差。決策只用下軌與中軌；上軌僅供圖表。預設 20、2 倍。",
        source_zh="John Bollinger 的通道。標準差 ddof=0。實作為本倉庫原創。",
        decision_indicators=["bb_lower", "bb_mid"],
        display_indicators=["bb_mid", "bb_upper", "bb_lower"],
        params=[
            _p("period", "週期", 20, 5, 200),
            _p("mult", "倍數", 2, 0.5, 4),
        ],
    ),
    "ma_cross": StrategySpec(
        id="ma_cross",
        name_zh="均線交叉",
        summary_zh="快均線上穿慢均線做多，下穿平倉。",
        formula_zh="兩條簡單移動平均。預設 5 與 20。決策指標就是這兩條。",
        source_zh="公開的均線交叉規則。實作為本倉庫原創，不是 backtesting.py 或 vectorbt 的程式。",
        decision_indicators=["ma_fast", "ma_slow"],
        display_indicators=["ma_fast", "ma_slow"],
        params=[
            _p("fast", "快線", 5, 2, 120),
            _p("slow", "慢線", 20, 3, 400),
        ],
    ),
    "grid": StrategySpec(
        id="grid",
        name_zh="等差網格",
        summary_zh="價格越靠近下界，目標倉位越高；越靠近上界越空倉。",
        formula_zh=(
            "若沒填上下界，用第一根收盤價 ± 幅度固定格線，不用全樣本最高最低，避免看未來。"
            "預設幅度 10%、5 格。曝險只在收盤跨過格線時改變。"
        ),
        source_zh="等差網格是公開交易概念。此實作不來自 freqtrade 或其他 GPL 專案。",
        decision_indicators=["grid_exposure"],
        display_indicators=["grid_lower", "grid_upper"],
        params=[
            _p("grid_lower", "下界（0 表示自動）", 0, 0, None, "0 則用第一根收盤 × (1−幅度)。"),
            _p("grid_upper", "上界（0 表示自動）", 0, 0, None, "0 則用第一根收盤 × (1+幅度)。"),
            _p("grid_span", "自動幅度", 0.10, 0.01, 0.5, "只有上下界為 0 時使用。"),
            _p("n_grids", "格數", 5, 2, 20),
        ],
    ),
    "rsi_macd": StrategySpec(
        id="rsi_macd",
        name_zh="RSI + MACD",
        summary_zh="收盤 MACD 金叉且 RSI 落在區間才做多；止損、止盈、死叉或 RSI 過熱，誰先到就出場。",
        formula_zh=(
            "進場：已收盤 K 上 MACD(12,26,9) 的 DIF 由下向上穿越 DEA，而且當根 RSI(14) 在 40 到 70（含兩端）。"
            "下一根開盤才買。出場看誰先發生：相對成交價止損 5%、止盈 10%、MACD 死叉、或 RSI 收盤大於 75。"
            "死叉與 RSI 出場用該根收盤價。同一根同時碰到止盈和止損時先止損。"
            "上面的週期和門檻都可以改。決策指標是 RSI、DIF、DEA，柱狀圖只顯示。"
        ),
        source_zh="Wilder RSI（1978）與 Appel 的 MACD。進出場組合是本框架寫明的規則，不是從 GPL 策略檔複製。",
        decision_indicators=["rsi", "dif", "dea"],
        display_indicators=["rsi", "dif", "dea", "macd_hist"],
        params=[
            _p("rsi_period", "RSI 週期", 14, 2, 200),
            _p("rsi_low", "RSI 進場下緣", 40, 1, 98, "金叉當根 RSI 必須大於等於這一條。"),
            _p("rsi_high", "RSI 進場上緣", 70, 2, 99, "金叉當根 RSI 必須小於等於這一條。"),
            _p("rsi_exit", "RSI 出場", 75, 3, 99, "收盤高於這一條就出場。"),
            _p("macd_fast", "MACD 快線", 12, 2, 100),
            _p("macd_slow", "MACD 慢線", 26, 3, 300),
            _p("macd_signal", "MACD 訊號", 9, 2, 100),
        ],
    ),
}


def list_strategies() -> list[dict]:
    return [spec.to_public() for spec in STRATEGIES.values()]


def get_strategy(strategy_id: str) -> StrategySpec:
    try:
        return STRATEGIES[strategy_id]
    except KeyError as exc:
        raise ConfigError(f"沒有這個策略：{strategy_id}") from exc


def _num(params: dict, key: str, spec: ParamSpec) -> float:
    raw = params.get(key, spec.default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{spec.label_zh} 不是數字") from exc
    if spec.min is not None and value < spec.min:
        raise ConfigError(f"{spec.label_zh} 不能小於 {spec.min}")
    if spec.max is not None and value > spec.max:
        raise ConfigError(f"{spec.label_zh} 不能大於 {spec.max}")
    return value


def resolve_params(strategy_id: str, params: dict | None) -> dict[str, float]:
    spec = get_strategy(strategy_id)
    params = params or {}
    unknown = set(params) - {p.key for p in spec.params}
    if unknown:
        raise ConfigError("策略沒有這些參數：" + "、".join(sorted(unknown)))
    return {p.key: _num(params, p.key, p) for p in spec.params}


def _nan_list(arr: np.ndarray) -> list[float | None]:
    return [None if not np.isfinite(v) else float(v) for v in arr]


def compute_signals(strategy_id: str, closes: list[float], params: dict | None, allow_short: bool = False):
    """回傳 (signals, indicators, warnings)。signals 是收盤當下想持有的曝險。"""
    spec = get_strategy(strategy_id)
    if len(spec.decision_indicators) > 3:
        raise ConfigError(f"{spec.name_zh} 的決策指標超過 3 個")
    resolved = resolve_params(strategy_id, params)
    close = np.asarray(closes, dtype=float)
    warnings: list[str] = []
    indicators: dict[str, list] = {}

    if strategy_id == "rsi_single":
        period = int(resolved["period"])
        line = rsi(close, period)
        signals = rsi_threshold_signal(line, resolved["oversold"], resolved["overbought"], allow_short)
        indicators["rsi"] = _nan_list(line)
    elif strategy_id == "rsi_three":
        short_p = int(resolved["rsi_short"])
        mid_p = int(resolved["rsi_mid"])
        long_p = int(resolved["rsi_long"])
        if not (short_p < mid_p < long_p):
            warnings.append("短、中、長週期沒有由小到大。觸發線仍是「短週期」欄位，程式沒有重新排序。")
        s_line = rsi(close, short_p)
        m_line = rsi(close, mid_p)
        l_line = rsi(close, long_p)
        signals = rsi_three_signal(s_line, m_line, l_line, allow_short)
        indicators["rsi_short"] = _nan_list(s_line)
        indicators["rsi_mid"] = _nan_list(m_line)
        indicators["rsi_long"] = _nan_list(l_line)
    elif strategy_id == "macd":
        fast, slow, signal_p = int(resolved["fast"]), int(resolved["slow"]), int(resolved["signal"])
        dif, dea, hist = macd(close, fast, slow, signal_p)
        signals = state_cross(dif, dea, allow_short)
        indicators["dif"] = _nan_list(dif)
        indicators["dea"] = _nan_list(dea)
        indicators["macd_hist"] = _nan_list(hist)
    elif strategy_id == "bollinger":
        period = int(resolved["period"])
        mid, upper, lower = bollinger(close, period, resolved["mult"])
        # 現貨回歸不做空；allow_short 不讓布林去追空，避免和規則說明不一致。
        if allow_short:
            warnings.append("布林回歸這個版本只做多後平倉，允許做空不會讓它在上軌放空。")
        signals = bollinger_reversion_signal(close, lower, mid)
        indicators["bb_mid"] = _nan_list(mid)
        indicators["bb_upper"] = _nan_list(upper)
        indicators["bb_lower"] = _nan_list(lower)
    elif strategy_id == "ma_cross":
        fast_p, slow_p = int(resolved["fast"]), int(resolved["slow"])
        if fast_p >= slow_p:
            raise ConfigError("均線快線週期必須小於慢線")
        fast_line, slow_line = sma(close, fast_p), sma(close, slow_p)
        signals = state_cross(fast_line, slow_line, allow_short)
        indicators["ma_fast"] = _nan_list(fast_line)
        indicators["ma_slow"] = _nan_list(slow_line)
    elif strategy_id == "grid":
        if allow_short:
            warnings.append("網格這個版本只在 0 到 1 之間調整多單，不會放空。")
        lower, upper = resolved["grid_lower"], resolved["grid_upper"]
        if lower == 0 and upper == 0:
            first = next((float(x) for x in close if np.isfinite(x)), None)
            if first is None:
                raise ConfigError("沒有可用收盤價，無法建立網格")
            span = resolved["grid_span"]
            lower, upper = first * (1 - span), first * (1 + span)
            warnings.append(
                f"網格上下界用第一根收盤 {first:.6g} 的 ±{span:.0%} 固定，沒有用全樣本高低點。"
            )
        elif lower <= 0 or upper <= 0:
            raise ConfigError("網格上下界要嘛都填正數，要嘛都填 0 表示自動")
        n_grids = int(resolved["n_grids"])
        exposure = grid_exposure(close, lower, upper, n_grids)
        signals = exposure
        indicators["grid_lower"] = [float(lower)] * len(close)
        indicators["grid_upper"] = [float(upper)] * len(close)
        indicators["grid_exposure"] = _nan_list(exposure)
    elif strategy_id == "rsi_macd":
        period = int(resolved["rsi_period"])
        low, high, exit_level = resolved["rsi_low"], resolved["rsi_high"], resolved["rsi_exit"]
        if not (0 < low < high < exit_level < 100):
            raise ConfigError("RSI 進場下緣要小於上緣，而且兩者都要低於出場線，並落在 0 到 100 之間")
        fast = int(resolved["macd_fast"])
        slow = int(resolved["macd_slow"])
        signal_p = int(resolved["macd_signal"])
        line = rsi(close, period)
        dif, dea, hist = macd(close, fast, slow, signal_p)
        entry = np.zeros(len(close))
        ind_exit = np.zeros(len(close))
        for i in range(1, len(close)):
            if not (_finite_pair(dif, dea, i) and np.isfinite(line[i])):
                continue
            golden = dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
            death = dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]
            if golden and low <= float(line[i]) <= high:
                entry[i] = 1.0
            if death or float(line[i]) > exit_level:
                ind_exit[i] = 1.0
        signals = entry
        indicators["rsi"] = _nan_list(line)
        indicators["dif"] = _nan_list(dif)
        indicators["dea"] = _nan_list(dea)
        indicators["macd_hist"] = _nan_list(hist)
        indicators["_indicator_exit"] = [float(v) for v in ind_exit]
        warnings.append("RSI+MACD 的進場是單根脈衝，持倉直到止盈、止損或指標出場，不是每根都重算目標曝險。")
    else:
        raise ConfigError(f"沒有這個策略：{strategy_id}")

    if allow_short and strategy_id in {"bollinger", "grid"}:
        pass
    return [float(x) for x in signals], indicators, warnings
