"""可選的 Polymarket 官方 SDK。套件名 polymarket-client，授權 MIT，上游標成 beta。

沒有安裝時拋 SdkUnavailable，由 data.py 退回既有的 httpx。不讀私鑰，也不建立 SecureClient。
"""

from __future__ import annotations


class SdkUnavailable(ImportError):
    """polymarket-client 不在環境裡。"""


def load_public_history(symbol: str, *, client=None) -> tuple[dict, dict]:
    if client is None:
        try:
            from polymarket import PublicClient
        except ImportError as exc:
            raise SdkUnavailable("polymarket-client 沒有安裝") from exc
        opened = PublicClient()
        if hasattr(opened, "__enter__"):
            with opened as live:
                return _read(live, symbol)
        return _read(opened, symbol)
    return _read(client, symbol)


def _read(sdk, symbol: str) -> tuple[dict, dict]:
    if str(symbol).isdigit():
        market = sdk.get_market(id=str(symbol))
    else:
        market = sdk.get_market(slug=str(symbol))
    token, outcome = _token(market)
    pages = sdk.list_price_history(asset_id=token, interval="max", bucket_seconds=86400)
    history = {"history": [{"t": _seconds(point), "p": _price(point)} for point in _points(pages)]}
    question = _attr(market, "question") or _attr(market, "title")
    return history, {
        "question": question,
        "outcome": outcome,
        "token_id": token,
        "adapter": "polymarket-sdk",
        "beta": True,
    }


def _attr(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return None


def _token(market) -> tuple[str, str]:
    outcomes = _attr(market, "outcomes")
    yes = _attr(outcomes, "yes") if outcomes is not None else None
    token = _attr(yes, "token_id", "tokenId") if yes is not None else None
    name = _attr(yes, "name", "outcome") if yes is not None else None
    if token:
        return str(token), str(name or "Yes")
    raw_ids = _attr(market, "clobTokenIds", "clob_token_ids")
    if isinstance(raw_ids, str):
        import json

        raw_ids = json.loads(raw_ids)
    names = _attr(market, "outcome_names")
    if raw_ids:
        label = names[0] if isinstance(names, (list, tuple)) and names else "index0"
        return str(raw_ids[0]), str(label)
    raise ValueError("官方 SDK 的市場沒有結果 token，不用現價充當歷史。")


def _points(pages) -> list:
    if hasattr(pages, "first_page"):
        page = pages.first_page()
        return list(getattr(page, "items", []) or [])
    if isinstance(pages, list):
        return pages
    return list(getattr(pages, "items", []) or [])


def _seconds(point) -> int:
    raw = _attr(point, "timestamp", "t")
    ts = int(raw)
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def _price(point) -> float:
    return float(_attr(point, "price", "p"))
