"""市場目錄。來源清單是明示的，不是失敗後的備援順序。"""

from __future__ import annotations

CATALOG = [
    {
        "id": "crypto_spot",
        "label_zh": "加密現貨",
        "instrument": "spot",
        "sources": ["okx", "binance", "kraken", "ccxt"],
        "default_source": "okx",
        "symbols": ["BTC/USDT", "BTC/USD", "ETH/USDT"],
        "timeframes": ["1d", "4h", "1h", "15m"],
        "leverage": [1],
        "currency": "USDT",
        "default_capital": 10,
        "default_fee_bps": 10,
        "note_zh": "現貨槓桿固定 1。10 美元是預設研究本金，可以改。ccxt 是可選來源，要另裝 ccxt 並帶 ccxt_exchange，不會取代 okx、binance、kraken。",
    },
    {
        "id": "crypto_perp",
        "label_zh": "加密永續（模擬保證金）",
        "instrument": "perpetual",
        "sources": ["okx", "binance"],
        "default_source": "okx",
        "symbols": ["BTC/USDT", "ETH/USDT"],
        "timeframes": ["1d", "4h", "1h", "15m"],
        "leverage": [1, 1.5, 2],
        "currency": "USDT",
        "default_capital": 10,
        "default_fee_bps": 4,
        "note_zh": "槓桿只在這個模式出現，上限 2 倍，不追加保證金。必須同時拿到資金費，拿不到就整筆失敗。",
    },
    {
        "id": "ashare",
        "label_zh": "A 股日線",
        "instrument": "spot",
        "sources": ["sina", "tencent", "eastmoney"],
        "default_source": "sina",
        "symbols": ["600519"],
        "timeframes": ["1d"],
        "leverage": [1],
        "currency": "CNY",
        "default_capital": 200000,
        "default_fee_bps": 2.5,
        "default_stamp_tax_bps": 5,
        "lot_size": 100,
        "note_zh": "只有日線。一手 100 股。買進那根不能賣（T+1）。漲停不買、跌停不賣；ST 5%，創業板與科創板 20%，主板 10%。停牌日不成交。茅台一手遠高於 10 美元，本金不足會明示，不會用零股或 yfinance 代替。",
    },
    {
        "id": "solana_pool",
        "label_zh": "Solana 池 K 線",
        "instrument": "spot",
        "sources": ["geckoterminal"],
        "default_source": "geckoterminal",
        "symbols": ["Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"],
        "timeframes": ["1d", "15m"],
        "leverage": [1],
        "currency": "USD",
        "default_capital": 10,
        "default_fee_bps": 30,
        "note_zh": "GeckoTerminal 的池 OHLCV。預設 30bp 是假設的池費，不是逐筆實測。熱門池有生存者偏差。",
    },
    {
        "id": "polymarket",
        "label_zh": "Polymarket",
        "instrument": "spot",
        "sources": ["polymarket"],
        "default_source": "polymarket",
        "symbols": ["559651"],
        "timeframes": ["1d"],
        "leverage": [1],
        "currency": "USDC",
        "default_capital": 10,
        "default_fee_bps": 0,
        "note_zh": "預設仍是 Gamma 與 CLOB prices-history（httpx）。可選 adapter=sdk 會試官方 polymarket-client（MIT，beta）；沒裝就退回 httpx。歷史是成交價點，不是 K 線高低。被 451 擋住就停止，不編價格。",
    },
    {
        "id": "stock_app",
        "label_zh": "Stock App 打金狗（尚未識別）",
        "instrument": "spot",
        "sources": [],
        "default_source": "",
        "symbols": [],
        "timeframes": [],
        "leverage": [],
        "currency": "",
        "default_capital": 10,
        "note_zh": "產品身分未知。這裡只有介面，不會用 yfinance、A 股或 GMGN 頂替。",
    },
]


def get_market(market_id: str) -> dict:
    for item in CATALOG:
        if item["id"] == market_id:
            return item
    raise KeyError(market_id)
