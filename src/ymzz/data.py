"""公開、免金鑰的行情適配器。任一來源失敗就停止，不悄悄換下一個。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ymzz.catalog import get_market
from ymzz.engine import Bar, Funding
from ymzz.errors import ConfigError, DataGapError, DataSourceError, StockAppUnidentified
from ymzz.http_cache import DataClient
from ymzz.timeutil import CST, UTC, ensure_utc

OKX_BAR = {"1d": "1D", "4h": "4H", "1h": "1H", "15m": "15m"}
BINANCE_INTERVAL = {"1d": "1d", "4h": "4h", "1h": "1h", "15m": "15m"}
KRAKEN_MINUTES = {"1d": 1440, "4h": 240, "1h": 60, "15m": 15}
DURATION = {
    "1d": timedelta(days=1),
    "4h": timedelta(hours=4),
    "1h": timedelta(hours=1),
    "15m": timedelta(minutes=15),
}


def _dt_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _dt_s(sec: int) -> datetime:
    return datetime.fromtimestamp(sec, tz=UTC)


def _parse_date(text: str, tz) -> datetime:
    day = datetime.strptime(text[:10], "%Y-%m-%d")
    return day.replace(tzinfo=tz)


def _ashare_bar(day: str, open_, high, low, close, volume, quote, now: datetime) -> Bar:
    start = _parse_date(day, CST).replace(hour=9, minute=30)
    end = _parse_date(day, CST).replace(hour=15, minute=0)
    closed = ensure_utc(now) >= ensure_utc(end)
    return Bar(
        ensure_utc(start),
        ensure_utc(end),
        float(open_),
        float(high),
        float(low),
        float(close),
        None if volume is None else float(volume),
        None if quote is None else float(quote),
        closed,
    )


def parse_sina(payload, now: datetime) -> tuple[list[Bar], int]:
    if not isinstance(payload, list):
        raise DataSourceError("新浪回應不是 K 線陣列")
    bars = []
    for row in payload:
        bars.append(
            _ashare_bar(row["day"], row["open"], row["high"], row["low"], row["close"], row.get("volume"), None, now)
        )
    return _finish_bars(bars, "Asia/Shanghai", "新浪日線沒有復權參數，不把這份資料稱作前復權。成交量單位是股，沒有成交額。")


def parse_tencent(payload, symbol: str, now: datetime) -> tuple[list[Bar], dict]:
    if payload.get("code") not in (0, "0"):
        raise DataSourceError(f"騰訊回應 code={payload.get('code')}，沒有改問新浪。")
    node = (payload.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day")
    if not rows:
        raise DataSourceError("騰訊沒有回傳日線")
    adjust = "qfq" if node.get("qfqday") else "none"
    bars = []
    for row in rows:
        # 日期、開、收、高、低、量（手）
        shares = float(row[5]) * 100.0
        bars.append(_ashare_bar(row[0], row[1], row[3], row[4], row[2], shares, None, now))
    bars, dropped = _split_closed(bars)
    meta = {
        "timezone": "Asia/Shanghai",
        "adjust": adjust,
        "volume_unit": "share",
        "volume_unit_original": "lot_100",
        "dropped_unclosed": dropped,
        "note_zh": "騰訊這次請求的是前復權。成交量由手換算成股（×100），沒有成交額。",
    }
    return bars, meta


def parse_eastmoney(payload, now: datetime) -> tuple[list[Bar], dict]:
    if payload.get("rc") not in (0, "0"):
        raise DataSourceError(f"東財 rc={payload.get('rc')}，沒有更換主機重試。")
    rows = ((payload.get("data") or {}).get("klines")) or []
    if not rows:
        raise DataSourceError("東財沒有回傳 K 線")
    bars = []
    for line in rows:
        parts = line.split(",")
        shares = float(parts[5]) * 100.0
        amount = float(parts[6])
        # 日期、開、收、高、低、量（手）、成交額
        bars.append(_ashare_bar(parts[0], parts[1], parts[3], parts[4], parts[2], shares, amount, now))
    bars, dropped = _split_closed(bars)
    meta = {
        "timezone": "Asia/Shanghai",
        "adjust": "qfq",
        "fqt": 1,
        "volume_unit": "share",
        "volume_unit_original": "lot_100",
        "dropped_unclosed": dropped,
        "note_zh": "東財 fqt=1 為前復權。量是手，已×100 成股；成交額沿用來源的金額。",
    }
    return bars, meta


def _split_closed(bars: list[Bar]) -> tuple[list[Bar], int]:
    closed = [bar for bar in bars if bar.closed and bar.high >= bar.low and bar.open > 0 and bar.close > 0]
    dropped = len(bars) - len(closed)
    closed.sort(key=lambda bar: bar.open_time)
    dedup = {}
    for bar in closed:
        dedup[bar.open_time] = bar
    return [dedup[key] for key in sorted(dedup)], dropped


def _finish_bars(bars: list[Bar], tz: str, note: str):
    closed, dropped = _split_closed(bars)
    return closed, {"timezone": tz, "dropped_unclosed": dropped, "note_zh": note}


def parse_okx_candles(payload, timeframe: str, now: datetime) -> tuple[list[Bar], dict]:
    if str(payload.get("code")) != "0":
        raise DataSourceError(f"OKX code={payload.get('code')} {payload.get('msg')}，沒有改走幣安。")
    duration = DURATION[timeframe]
    bars = []
    dropped = 0
    for row in payload.get("data") or []:
        opened = _dt_ms(int(row[0]))
        confirm = str(row[8]) if len(row) > 8 else ""
        is_closed = confirm == "1" if confirm != "" else ensure_utc(now) >= opened + duration
        if not is_closed:
            dropped += 1
            continue
        quote = float(row[7]) if len(row) > 7 else None
        bars.append(
            Bar(opened, opened + duration, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), quote, True)
        )
    bars, extra = _split_closed(bars)
    tz = "Asia/Shanghai" if timeframe == "1d" else "UTC"
    return bars, {
        "timezone": tz,
        "dropped_unclosed": dropped + extra,
        "note_zh": "OKX 日線以 UTC+8 0 點切開，和幣安／Kraken 的 UTC 日線不同。confirm=0 的未收盤 K 已剔除。數量後續用幣價換算，不用合約張數當持倉。",
    }


def parse_okx_funding(payload) -> list[Funding]:
    if str(payload.get("code")) != "0":
        raise DataSourceError(f"OKX 資金費 code={payload.get('code')}，永續回測因此停止，不會把資金費當 0。")
    events = []
    for row in payload.get("data") or []:
        rate = row.get("fundingRate")
        when = row.get("fundingTime")
        if rate is None or when is None:
            continue
        events.append(Funding(_dt_ms(int(when)), float(rate)))
    events.sort(key=lambda item: item.time)
    return events


def parse_binance_klines(payload, timeframe: str, now: datetime) -> tuple[list[Bar], dict]:
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise DataSourceError(
            f"幣安拒絕這次請求：{payload.get('msg') or payload}",
            status_code=payload.get("code"),
        )
    if not isinstance(payload, list):
        raise DataSourceError("幣安 K 線不是陣列，沒有改用其他交易所。")
    duration = DURATION[timeframe]
    bars = []
    dropped = 0
    for row in payload:
        opened = _dt_ms(int(row[0]))
        close_time = _dt_ms(int(row[6]))
        if ensure_utc(now) < close_time or ensure_utc(now) < opened + duration:
            dropped += 1
            continue
        bars.append(
            Bar(
                opened,
                opened + duration,
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
                float(row[7]),
                True,
            )
        )
    bars, extra = _split_closed(bars)
    return bars, {
        "timezone": "UTC",
        "dropped_unclosed": dropped + extra,
        "note_zh": "幣安日線以 UTC 0 點切開。未收盤的最後一根已剔除。",
    }


def parse_binance_funding(payload) -> list[Funding]:
    if isinstance(payload, dict):
        raise DataSourceError(f"幣安資金費失敗：{payload.get('msg') or payload}。不會改用 OKX 的資金費。")
    return [Funding(_dt_ms(int(row["fundingTime"])), float(row["fundingRate"])) for row in payload]


def parse_kraken_ohlc(payload, timeframe: str, now: datetime) -> tuple[list[Bar], dict]:
    if payload.get("error"):
        raise DataSourceError(f"Kraken 錯誤：{payload.get('error')}")
    result = payload.get("result") or {}
    rows = None
    for key, value in result.items():
        if key != "last":
            rows = value
            break
    if not rows:
        raise DataSourceError("Kraken 沒有 OHLC")
    duration = DURATION[timeframe]
    bars = []
    dropped = 0
    for row in rows:
        opened = _dt_s(int(row[0]))
        if ensure_utc(now) < opened + duration:
            dropped += 1
            continue
        bars.append(
            Bar(opened, opened + duration, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[6]), None, True)
        )
    bars, extra = _split_closed(bars)
    return bars, {
        "timezone": "UTC",
        "dropped_unclosed": dropped + extra,
        "note_zh": "Kraken 日線以 UTC 切開。公開 OHLC 的 count 參數不可靠，這裡用時間過濾，不用它。沒有成交額。",
    }


def parse_gecko_ohlcv(payload, timeframe: str, now: datetime) -> tuple[list[Bar], dict]:
    rows = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    if not rows:
        raise DataSourceError("GeckoTerminal 沒有 OHLCV，沒有改用 Dexscreener 的現況快照充當歷史。")
    duration = DURATION[timeframe]
    bars = []
    dropped = 0
    for row in rows:
        opened = _dt_s(int(row[0]))
        if ensure_utc(now) < opened + duration:
            dropped += 1
            continue
        bars.append(
            Bar(opened, opened + duration, float(row[1]), float(row[2]), float(row[3]), float(row[4]), None, float(row[5]), True)
        )
    bars, extra = _split_closed(bars)
    meta_attr = payload.get("meta") or {}
    return bars, {
        "timezone": "UTC",
        "dropped_unclosed": dropped + extra,
        "base": (meta_attr.get("base") or {}).get("symbol"),
        "quote": (meta_attr.get("quote") or {}).get("symbol"),
        "note_zh": "GeckoTerminal 池 K 線。成交量欄是美元成交額。熱榜池有生存者偏差，這條序列不是當時可交易全集。",
    }


def parse_polymarket_history(payload, now: datetime) -> tuple[list[Bar], dict]:
    if isinstance(payload, dict) and payload.get("error"):
        raise DataSourceError(f"Polymarket 歷史價錯誤：{payload.get('error')}。沒有編造價格。")
    history = payload.get("history") if isinstance(payload, dict) else None
    if not history:
        raise DataSourceError("Polymarket prices-history 是空的。拒絕用當前報價補成歷史曲線。")
    bars = []
    for row in history:
        when = _dt_s(int(row["t"]))
        if ensure_utc(when) > ensure_utc(now):
            continue
        price = float(row["p"])
        bars.append(Bar(when, when, price, price, price, price, None, None, True))
    bars, dropped = _split_closed(bars)
    return bars, {
        "timezone": "UTC",
        "dropped_unclosed": dropped,
        "bar_kind": "price_point",
        "note_zh": "這是價格點，高=低=收，沒有日內高低。止盈止損只能看點是否穿過，不能假裝有 K 線影線。市值不是事件結算賠付。",
    }


def _split_symbol(symbol: str) -> tuple[str, str]:
    text = symbol.replace("-", "/").upper()
    if "/" in text:
        base, quote = text.split("/", 1)
        return base, quote
    for quote in ("USDT", "USDC", "USD"):
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)], quote
    raise ConfigError(f"無法拆開交易對：{symbol}")


def _range_filter(bars: list[Bar], start: datetime | None, end: datetime | None, tz_name: str) -> list[Bar]:
    tz = CST if tz_name == "Asia/Shanghai" else UTC
    kept = []
    for bar in bars:
        local_day = bar.open_time.astimezone(tz).date()
        if start is not None and local_day < start.astimezone(tz).date():
            continue
        if end is not None and local_day > end.astimezone(tz).date():
            continue
        kept.append(bar)
    return kept


def _require_coverage(bars: list[Bar], start: datetime | None, tz_name: str, source: str):
    if not bars:
        raise DataSourceError(f"{source} 在剔除未收盤之後沒有 K 線")
    if start is None:
        return
    tz = CST if tz_name == "Asia/Shanghai" else UTC
    earliest = bars[0].open_time.astimezone(tz).date()
    if earliest > start.astimezone(tz).date():
        raise DataGapError(
            f"{source} 最早只有 {earliest.isoformat()}，早於請求起點 {start.astimezone(tz).date().isoformat()} 的部分不存在。沒有用別的來源補洞。",
            earliest=earliest.isoformat(),
            latest=bars[-1].open_time.astimezone(tz).date().isoformat(),
        )


def _date(text: str | None, tz) -> datetime | None:
    if not text:
        return None
    return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=tz)


class StockAppAdapter:
    """身分未定的佔位。不要在這裡接 yfinance。"""

    market_id = "stock_app"

    def load(self, *_args, **_kwargs):
        raise StockAppUnidentified(
            "Stock App 打金狗的產品身分還沒有被指認。請提供官方網址、商店頁或 Telegram 機器人名稱。"
            "在那之前，這個適配器不會用 yfinance、A 股行情或 GMGN 來頂替，也不會產生假回測。"
        )


def load_market(spec: dict, client: DataClient, now: datetime | None = None):
    now = now or datetime.now(UTC)
    market = spec["market"]
    if market == "stock_app":
        raise StockAppUnidentified(
            "Stock App 打金狗的產品身分還沒有被指認。請提供官方網址、商店頁或 Telegram 機器人名稱。"
            "在那之前不會用 yfinance、A 股或 GMGN 頂替。"
        )
    try:
        meta_market = get_market(market)
    except KeyError as exc:
        raise ConfigError(f"沒有這個市場：{market}") from exc
    source = spec.get("source") or meta_market["default_source"]
    if source not in meta_market["sources"]:
        raise ConfigError(f"{meta_market['label_zh']} 沒有來源 {source}。失敗時也不會自動改接別的來源。")
    timeframe = spec.get("timeframe") or "1d"
    if timeframe not in meta_market["timeframes"]:
        raise ConfigError(f"這個市場目前沒有週期 {timeframe}。A 股只做已核對的日線，不會做成分鐘假資料。")
    mode = spec.get("data_mode") or "live"
    start = _date(spec.get("start"), CST if market == "ashare" else UTC)
    end = _date(spec.get("end"), CST if market == "ashare" else UTC)
    if market == "ashare":
        bars, extra, prov = _load_ashare(source, spec["symbol"], start, end, client, now, mode, spec.get("source_host"))
        funding: list[Funding] = []
    elif market in {"crypto_spot", "crypto_perp"}:
        instrument = "perpetual" if market == "crypto_perp" else "spot"
        bars, funding, extra, prov = _load_crypto(
            source, spec["symbol"], timeframe, instrument, start, end, client, now, mode
        )
    elif market == "solana_pool":
        bars, extra, prov = _load_gecko(spec["symbol"], timeframe, start, end, client, now, mode)
        funding = []
    elif market == "polymarket":
        bars, extra, prov = _load_polymarket(spec["symbol"], start, end, client, now, mode)
        funding = []
    else:
        raise ConfigError(f"沒有這個市場：{market}")
    tz_name = extra.get("timezone") or "UTC"
    if spec.get("strict_range", True):
        _require_coverage(bars, start, tz_name, source)
    bars = _range_filter(bars, start, end, tz_name)
    if not bars:
        raise DataSourceError(f"{source} 在請求區間內沒有已收盤 K 線")
    provenance = {**prov, **extra, "source": source, "market": market, "symbol": spec.get("symbol"), "timeframe": timeframe}
    return bars, funding, provenance


def _load_ashare(source, symbol, start, end, client: DataClient, now, mode, host):
    code = symbol.lower().replace("sh", "").replace("sz", "")
    if not code.isdigit() or len(code) != 6:
        raise ConfigError("A 股代碼請用 6 位數字，例如 600519")
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    if code.startswith(("4", "8")):
        raise ConfigError("這版只接滬深 6 位股票代碼，北交所沒有核對過，所以不猜。")
    limit = 500
    if source == "sina":
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": f"{prefix}{code}", "scale": "240", "ma": "no", "datalen": str(limit)}
        payload, prov = client.get_json(url, params, mode=mode, headers={"Referer": "https://finance.sina.com.cn"})
        bars, extra = parse_sina(payload, now)
        return bars, extra, prov
    if source == "tencent":
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = {"param": f"{prefix}{code},day,,,{limit},qfq"}
        payload, prov = client.get_json(url, params, mode=mode)
        bars, extra = parse_tencent(payload, f"{prefix}{code}", now)
        return bars, extra, prov
    if source == "eastmoney":
        host = host or "80.push2his.eastmoney.com"
        allowed = {"80.push2his.eastmoney.com", "92.push2his.eastmoney.com", "push2his.eastmoney.com"}
        if host not in allowed:
            raise ConfigError("東財主機不在允許清單。不會自動輪詢其他主機。")
        market = "1" if prefix == "sh" else "0"
        url = f"https://{host}/api/qt/stock/kline/get"
        params = {
            "secid": f"{market}.{code}",
            "klt": "101",
            "fqt": "1",
            "lmt": str(limit),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        payload, prov = client.get_json(url, params, mode=mode, headers={"User-Agent": "Mozilla/5.0"})
        bars, extra = parse_eastmoney(payload, now)
        extra["host"] = host
        return bars, extra, prov
    raise ConfigError(f"沒有這個 A 股來源：{source}")


def _paginate(fetch_page, newest_first_ts):
    rows = []
    cursor = None
    for _ in range(12):
        page = fetch_page(cursor)
        if not page:
            break
        rows.extend(page)
        oldest = min(newest_first_ts(row) for row in page)
        if cursor == oldest:
            break
        cursor = oldest
        if len(page) < 90:
            break
    return rows


def _load_crypto(source, symbol, timeframe, instrument, start, end, client: DataClient, now, mode):
    base, quote = _split_symbol(symbol)
    if source == "kraken":
        if instrument != "spot":
            raise ConfigError("Kraken 這版只有現貨 OHLC，不會把現貨當成永續。")
        if (base, quote) != ("BTC", "USD"):
            raise ConfigError("Kraken 這版只接 BTC/USD。其他交易對沒有對過代號，所以不猜。")
        url = "https://api.kraken.com/0/public/OHLC"
        payload, prov = client.get_json(url, {"pair": "XBTUSD", "interval": str(KRAKEN_MINUTES[timeframe])}, mode=mode)
        bars, extra = parse_kraken_ohlc(payload, timeframe, now)
        return bars, [], extra, prov
    if source == "okx":
        inst = f"{base}-{quote}" + ("-SWAP" if instrument == "perpetual" else "")
        bar = OKX_BAR[timeframe]

        def fetch_page(after):
            params = {"instId": inst, "bar": bar, "limit": "100"}
            if after is not None:
                params["after"] = str(after)
            payload, prov = client.get_json("https://www.okx.com/api/v5/market/history-candles", params, mode=mode)
            fetch_page.prov = prov
            if str(payload.get("code")) != "0":
                raise DataSourceError(f"OKX K 線失敗：{payload.get('msg')}。沒有改走幣安。")
            return payload.get("data") or []

        fetch_page.prov = {}
        raw_rows = _paginate(fetch_page, lambda row: int(row[0]))
        bars, extra = parse_okx_candles({"code": "0", "data": raw_rows}, timeframe, now)
        funding: list[Funding] = []
        if instrument == "perpetual":
            def fetch_funding(after):
                params = {"instId": inst, "limit": "100"}
                if after is not None:
                    params["after"] = str(after)
                payload, prov = client.get_json("https://www.okx.com/api/v5/public/funding-rate-history", params, mode=mode)
                fetch_funding.prov = prov
                return parse_okx_funding(payload), payload.get("data") or []

            fetch_funding.prov = {}
            events: list[Funding] = []
            cursor = None
            for _ in range(15):
                parsed, raw = fetch_funding(cursor)
                if not raw:
                    break
                events.extend(parsed)
                oldest = min(int(row["fundingTime"]) for row in raw)
                if cursor == oldest:
                    break
                cursor = oldest
                if len(raw) < 90:
                    break
            # 去重
            uniq = {item.time: item for item in events}
            funding = [uniq[key] for key in sorted(uniq)]
            if not funding:
                raise DataSourceError("永續模式沒有拿到任何資金費，已停止。不會只拿 K 線把資金費當 0。")
            extra["funding_points"] = len(funding)
            extra["funding_sha256"] = fetch_funding.prov.get("sha256")
            if bars and funding and funding[0].time > bars[0].open_time + DURATION[timeframe]:
                extra["funding_gap"] = True
                extra["note_zh"] = extra.get("note_zh", "") + " 資金費歷史比 K 線短，較早的持倉沒有被填 0，只是沒有事件可扣。"
        return bars, funding, extra, fetch_page.prov
    if source == "binance":
        pair = f"{base}{quote}"
        interval = BINANCE_INTERVAL[timeframe]
        host = "https://fapi.binance.com" if instrument == "perpetual" else "https://api.binance.com"
        path = "/fapi/v1/klines" if instrument == "perpetual" else "/api/v3/klines"
        payload, prov = client.get_json(host + path, {"symbol": pair, "interval": interval, "limit": "1000"}, mode=mode)
        bars, extra = parse_binance_klines(payload, timeframe, now)
        funding = []
        if instrument == "perpetual":
            fund_payload, fund_prov = client.get_json(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                {"symbol": pair, "limit": "1000"},
                mode=mode,
            )
            funding = parse_binance_funding(fund_payload)
            if not funding:
                raise DataSourceError("幣安永續沒有資金費，已停止。")
            extra["funding_sha256"] = fund_prov["sha256"]
            extra["funding_points"] = len(funding)
        return bars, funding, extra, prov
    raise ConfigError(f"沒有這個加密來源：{source}")


def _load_gecko(pool: str, timeframe: str, start, end, client: DataClient, now, mode):
    kind = {"1d": "day", "15m": "minute"}[timeframe]
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{kind}"
    params = {"limit": "1000"}
    if timeframe == "15m":
        params["aggregate"] = "15"
    payload, prov = client.get_json(url, params, mode=mode, headers={"Accept": "application/json"})
    bars, extra = parse_gecko_ohlcv(payload, timeframe, now)
    return bars, extra, prov


def _load_polymarket(symbol: str, start, end, client: DataClient, now, mode):
    gamma_url = "https://gamma-api.polymarket.com/markets"
    if symbol.isdigit():
        payload, gamma_prov = client.get_json(f"https://gamma-api.polymarket.com/markets/{symbol}", mode=mode)
        market = payload[0] if isinstance(payload, list) else payload
    else:
        payload, gamma_prov = client.get_json(gamma_url, {"slug": symbol, "limit": "1"}, mode=mode)
        if not payload:
            raise DataSourceError(f"Gamma 找不到市場 {symbol}")
        market = payload[0]
    token_ids = market.get("clobTokenIds")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if not token_ids:
        raise DataSourceError("Gamma 市場沒有 clobTokenIds，無法要歷史價，也不用現價充當歷史。")
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    token = token_ids[0]
    outcome = outcomes[0] if outcomes else "index0"
    # CLOB 不接受過長的 startTs/endTs（會回 400 invalid filters）。
    # 先拿 interval=max，再在本地依日期過濾；不要把這次失敗換成另一個市場。
    params = {"market": token, "interval": "max", "fidelity": "1440"}
    try:
        history, hist_prov = client.get_json("https://clob.polymarket.com/prices-history", params, mode=mode)
    except DataSourceError as exc:
        if exc.extra.get("status_code") == 451:
            raise DataSourceError(
                "Polymarket prices-history 回 HTTP 451（這個網路被擋）。歷史回測停止，沒有用 Gamma 現價編一條曲線。",
                status_code=451,
                source_url="https://clob.polymarket.com/prices-history",
            ) from exc
        raise
    bars, extra = parse_polymarket_history(history, now)
    extra["question"] = market.get("question")
    extra["outcome"] = outcome
    extra["token_id"] = token
    extra["gamma_sha256"] = gamma_prov["sha256"]
    extra["note_zh"] = extra["note_zh"] + f" 標的是結果「{outcome}」的 token，不是 No。"
    return bars, extra, hist_prov
