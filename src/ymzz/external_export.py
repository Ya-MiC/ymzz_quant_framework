"""把研究用的 K 線與訊號寫成 CSV，給另外安裝的 Nautilus 或 Lean 自己讀。

這個倉庫不內嵌那兩個引擎。LGPL 的 Nautilus 與 Apache-2.0 的 Lean 都留在各自的環境。
"""

from __future__ import annotations

import csv
from pathlib import Path

from ymzz.engine import Bar


def export_research_csv(bars: list[Bar], signals: list[float], directory: str | Path) -> dict[str, str]:
    if len(bars) != len(signals):
        raise ValueError("訊號長度必須和 K 線相同")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    ohlcv = root / "ohlcv.csv"
    signal_path = root / "signals.csv"
    with ohlcv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow(
                [
                    bar.open_time.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    "" if bar.volume is None else bar.volume,
                ]
            )
    with signal_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "signal"])
        for bar, signal in zip(bars, signals):
            writer.writerow([bar.close_time.isoformat(), signal])
    return {"ohlcv": str(ohlcv), "signals": str(signal_path)}
