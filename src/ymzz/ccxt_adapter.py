"""可選的 ccxt 現貨 K 線。不取代 okx、binance、kraken 的既有解析。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ymzz.engine import Bar
from ymzz.errors import ConfigError, DataSourceError
from ymzz.timeutil import ensure_utc

_DURATION = {
    "1d": timedelta(days=1),
    "4h": timedelta(hours=4),
    "1h": timedelta(hours=1),
    "15m": timedelta(minutes=15),
}
_CCXT_TF = {"1d": "1d", "4h": "4h", "1h": "1h", "15m": "15m"}


def load_ccxt_bars(exchange_id: str | None, symbol: str, timeframe: str, now: datetime, *, fetch=None):
    if not exchange_id:
        raise ConfigError("ccxt 需要 ccxt_exchange，例如 okx。既有的 okx、binance、kraken 來源不用這個欄位，也不會改走 ccxt。")
    if timeframe not in _CCXT_TF:
        raise ConfigError(f"ccxt 這版沒有週期 {timeframe}")
    rows = (fetch or _ccxt_fetch)(exchange_id, symbol, _CCXT_TF[timeframe])
    duration = _DURATION[timeframe]
    bars: list[Bar] = []
    for row in rows or []:
        opened = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        close_time = opened + duration
        if ensure_utc(now) < close_time:
            continue
        bars.append(
            Bar(opened, close_time, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), None, True)
        )
    if not bars:
        raise DataSourceError(f"ccxt {exchange_id} 沒有已收盤 K 線。沒有改走其他交易所。")
    extra = {
        "timezone": "UTC",
        "adapter": "ccxt",
        "exchange": exchange_id,
        "dropped_unclosed": 0,
        "note_zh": f"ccxt 現貨適配器，交易所 {exchange_id}。okx、binance、kraken 的原路徑仍在，這條沒有蓋掉它們。",
    }
    return bars, extra


def _ccxt_fetch(exchange_id: str, symbol: str, timeframe: str):
    try:
        import ccxt
    except ImportError as exc:
        raise ConfigError("ccxt 沒有安裝。這是可選來源，okx、binance、kraken 的既有路徑不受影響。") from exc
    if not hasattr(ccxt, exchange_id):
        raise ConfigError(f"ccxt 沒有交易所 {exchange_id}")
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    return exchange.fetch_ohlcv(symbol, timeframe, limit=500)
