"""只讀的 Kraken 私有 REST。沒有密鑰時不連線。回應與錯誤文字都會去掉密鑰。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import httpx

from ymzz.redact import redact

API = "https://api.kraken.com"


def sign(url_path: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = url_path.encode() + hashlib.sha256(encoded).digest()
    digest = hmac.new(base64.b64decode(secret), message, hashlib.sha512).digest()
    return base64.b64encode(digest).decode()


def credentials_configured() -> bool:
    return bool(os.environ.get("KRAKEN_API_KEY")) and bool(os.environ.get("KRAKEN_API_SECRET"))


def _not_configured() -> dict:
    return {
        "configured": False,
        "ok": True,
        "status": "not configured",
        "message_zh": "尚未設定 KRAKEN_API_KEY / KRAKEN_API_SECRET。實盤面板不會連線。",
        "balances": [],
        "open_orders": [],
        "trades": [],
        "position": None,
        "equity": None,
        "unrealized_pnl": None,
        "realized_pnl": None,
    }


def _post(path: str, data: dict, poster) -> dict:
    key = os.environ.get("KRAKEN_API_KEY") or ""
    secret = os.environ.get("KRAKEN_API_SECRET") or ""
    payload = dict(data)
    payload["nonce"] = str(int(time.time() * 1000))
    headers = {
        "API-Key": key,
        "API-Sign": sign(path, payload, secret),
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "YMZZResearch/0.1 (local dashboard)",
    }
    if poster is None:
        response = httpx.post(API + path, data=payload, headers=headers, timeout=20.0)
        response.raise_for_status()
        body = response.json()
    else:
        body = poster(path, payload, headers)
    if not isinstance(body, dict):
        raise RuntimeError("Kraken 回應不是 JSON 物件")
    errors = body.get("error") or []
    if errors:
        raise RuntimeError(redact("; ".join(str(item) for item in errors)))
    return body.get("result") or {}


def _float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def snapshot_private(*, poster=None, ticker=None) -> dict:
    """餘額、未結委託、近期成交。poster 供測試注入，正式路徑才打 api.kraken.com。"""
    if not credentials_configured():
        return _not_configured()
    try:
        balances = _post("/0/private/Balance", {}, poster)
        orders = _post("/0/private/OpenOrders", {}, poster)
        trades = _post("/0/private/TradesHistory", {}, poster)
        trade_balance = _post("/0/private/TradeBalance", {"asset": "ZUSD"}, poster)
    except Exception as exc:
        return {
            "configured": True,
            "ok": False,
            "status": "error",
            "message_zh": redact(str(exc))[:300],
            "balances": [],
            "open_orders": [],
            "trades": [],
            "position": None,
            "equity": None,
            "unrealized_pnl": None,
            "realized_pnl": None,
        }
    balance_rows = [{"asset": name, "amount": amount} for name, amount in sorted(balances.items())]
    btc = 0.0
    for name, amount in balances.items():
        if name in {"XXBT", "XBT", "BTC"}:
            btc += float(amount)
    last = None
    if ticker is not None:
        last = ticker
    open_rows = []
    sl = None
    tp = None
    for txid, row in (orders.get("open") or {}).items():
        descr = row.get("descr") or {}
        kind = descr.get("ordertype") or ""
        price = _float(descr.get("price"))
        item = {
            "txid": txid,
            "pair": descr.get("pair"),
            "side": descr.get("type"),
            "ordertype": kind,
            "price": price,
            "volume": row.get("vol"),
        }
        open_rows.append(item)
        if kind == "stop-loss" and price is not None:
            sl = price
        if kind in {"take-profit", "take-profit-limit"} and price is not None:
            tp = price
    trade_rows = []
    cashflow = 0.0
    for txid, row in (trades.get("trades") or {}).items():
        cost = _float(row.get("cost")) or 0.0
        fee = _float(row.get("fee")) or 0.0
        side = row.get("type")
        if side == "sell":
            cashflow += cost - fee
        elif side == "buy":
            cashflow -= cost + fee
        trade_rows.append(
            {
                "txid": txid,
                "time": row.get("time"),
                "pair": row.get("pair"),
                "side": side,
                "price": row.get("price"),
                "vol": row.get("vol"),
                "cost": row.get("cost"),
                "fee": row.get("fee"),
                "ordertype": row.get("ordertype"),
            }
        )
    trade_rows.sort(key=lambda item: float(item.get("time") or 0), reverse=True)
    unrealized = _float(trade_balance.get("n"))
    if unrealized is None and last is not None and btc > 0:
        unrealized = None
    return {
        "configured": True,
        "ok": True,
        "status": "ok",
        "message_zh": "已用私有 API 讀取餘額與委託。這支程式沒有顯示密鑰。",
        "balances": balance_rows,
        "open_orders": open_rows,
        "trades": trade_rows[:20],
        "position": {
            "asset": "XBT",
            "qty": btc,
            "stop_loss": sl,
            "take_profit": tp,
            "last": last,
        },
        "equity": _float(trade_balance.get("eb")) or _float(trade_balance.get("e")),
        "unrealized_pnl": unrealized,
        "realized_pnl": cashflow,
        "realized_note_zh": "已實現數字是這次回傳的成交現金流（賣出減去買入與手續費），不是完整的稅務損益。",
    }
