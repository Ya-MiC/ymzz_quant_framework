"""只讀公開地址。轉帳不是損益，也沒有私鑰。"""

from __future__ import annotations

from datetime import datetime, timezone

from ymzz.engine import Bar
from ymzz.errors import ConfigError, WalletError
from ymzz.http_cache import DataClient

SOL_MINT = "So11111111111111111111111111111111111111112"
RPC_URL = "https://api.mainnet-beta.solana.com"

CAVEATS_ZH = [
    "只讀公開地址。沒有私鑰、沒有錢包連線、不會簽名或下單。",
    "轉帳、空投、手續費都不是交易損益，不會放進跟單回測。",
    "看不出這個地址是聰明錢、有人出資，還是詐騙。",
    "看不出代幣之後還賣不賣得掉。蜜罐、無流動性、凍結都不在這份資料裡。",
    "池子或做市程式的庫存變化不是某個自然人的跟單成績。",
    "延遲後的價格若取自 K 線開盤，仍然看不到夾子、失敗交易和優先費。",
]


def _ui(row: dict) -> float:
    amount = row.get("uiTokenAmount") or {}
    value = amount.get("uiAmount")
    if value is None:
        text = amount.get("uiAmountString")
        return float(text) if text not in (None, "") else 0.0
    return float(value)


def parse_transaction(payload: dict, address: str) -> dict:
    """把一筆 jsonParsed 交易對某個公開地址分類。"""
    if payload.get("error"):
        err = payload["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise WalletError(f"Solana RPC 拒絕這筆交易：{message}。沒有用別筆交易代替。")
    result = payload.get("result")
    if not result:
        raise WalletError("節點沒有回傳這筆交易（result 為空）。沒有拿別的簽名填洞。")
    meta = result.get("meta") or {}
    signature = (result.get("transaction") or {}).get("signatures", [None])[0]
    when = result.get("blockTime")
    stamp = datetime.fromtimestamp(when, tz=timezone.utc).isoformat() if when else None
    if meta.get("err"):
        return _event(signature, stamp, "failed", [], None, "交易失敗，不計入損益。", False)
    keys = ((result.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    index = None
    for i, key in enumerate(keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey == address:
            index = i
            break
    deltas: dict[str, float] = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for row in meta.get(side) or []:
            if row.get("owner") != address:
                continue
            mint = row.get("mint") or ""
            deltas[mint] = deltas.get(mint, 0.0) + sign * _ui(row)
    fee = int(meta.get("fee") or 0)
    sol_delta = 0.0
    if index is not None:
        sol_delta = (int(meta["postBalances"][index]) - int(meta["preBalances"][index])) / 1_000_000_000
        payer = keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
        if payer == address:
            sol_delta += fee / 1_000_000_000
    if abs(sol_delta) > 1e-9:
        deltas["SOL"] = deltas.get("SOL", 0.0) + sol_delta
    material = {mint: delta for mint, delta in deltas.items() if abs(delta) > 1e-9}
    assets = [{"mint": mint, "delta": delta} for mint, delta in material.items()]
    if not material:
        return _event(signature, stamp, "fee_only", [], None, "這個地址在這筆交易裡沒有資產增減，只有可能的手續費或完全沒參與庫存。不計損益。", False)
    mints = set(material)
    if mints <= {"SOL", SOL_MINT} and "SOL" in mints and SOL_MINT in mints:
        return _event(signature, stamp, "wrap", assets, None, "原生 SOL 與 wSOL 對沖，視為包裝或拆包，不是兌換損益。", False)
    ups = [m for m, d in material.items() if d > 0]
    downs = [m for m, d in material.items() if d < 0]
    if len(ups) == 1 and len(downs) == 1:
        received, paid = ups[0], downs[0]
        price = abs(material[received] / material[paid]) if material[paid] else None
        # 價格定義成「付出資產每 1 單位換到多少收到資產」的倒數，若付出的是報價資產則另算。
        implied = abs(material[paid] / material[received]) if material[received] else None
        note = f"一進一出，視為兌換。付出 {paid}，收到 {received}。這不能證明之後賣得掉。"
        return _event(
            signature,
            stamp,
            "swap",
            assets,
            implied,
            note,
            True,
            paid_mint=paid,
            received_mint=received,
            paid_qty=abs(material[paid]),
            received_qty=abs(material[received]),
        )
    if ups and downs:
        return _event(signature, stamp, "swap_ambiguous", assets, None, "資產變動超過兩個，無法可靠還原單一價格，不拿去跟單。", False)
    return _event(signature, stamp, "transfer", assets, None, "只有單向增減，視為轉帳或空投，不是交易損益。", False)


def _event(signature, stamp, kind, assets, implied, note, pnl, **extra):
    return {
        "signature": signature,
        "time": stamp,
        "kind": kind,
        "assets": assets,
        "implied_price": implied,
        "note_zh": note,
        "pnl_relevant": pnl,
        **extra,
    }


def inspect_address(address: str, client: DataClient, limit: int = 10, mode: str = "live") -> dict:
    address = (address or "").strip()
    if len(address) < 32 or len(address) > 48:
        raise WalletError("請輸入 Solana 公開地址。不要貼私鑰、助記詞或簽章。")
    limit = max(1, min(int(limit), 20))
    payload, prov = client.post_json(
        RPC_URL,
        {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [address, {"limit": limit}]},
        mode=mode,
        headers={"content-type": "application/json"},
    )
    if payload.get("error"):
        raise WalletError(f"讀取簽名失敗：{payload['error']}。沒有改用需要金鑰的瀏覽器 API。")
    events = []
    errors = []
    for item in payload.get("result") or []:
        signature = item.get("signature")
        if not signature:
            continue
        try:
            tx, tx_prov = client.post_json(
                RPC_URL,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
                },
                mode=mode,
                headers={"content-type": "application/json"},
            )
            event = parse_transaction(tx, address)
            event["tx_sha256"] = tx_prov["sha256"]
            events.append(event)
        except WalletError as exc:
            errors.append({"signature": signature, "message_zh": exc.message_zh})
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return {
        "address": address,
        "events": events,
        "errors": errors,
        "counts": counts,
        "caveats_zh": CAVEATS_ZH,
        "provenance": prov,
        "read_only": True,
    }


def replay_swaps(
    events: list[dict],
    bars: list[Bar],
    *,
    delay_seconds: float,
    slippage_bps: float,
    capital: float,
    base_mint: str,
) -> dict:
    """用延遲之後的 K 線開盤價重播兌換。沒有 K 線就不做，也不編價格。"""
    if delay_seconds < 0 or slippage_bps < 0 or capital <= 0:
        raise ConfigError("延遲、滑點與本金必須是合理的正數（延遲可以是 0）")
    if not bars:
        raise WalletError("沒有價格路徑。拒絕用對方的成交價假裝是延遲之後的市場價格。")
    if not base_mint:
        raise ConfigError("跟單要指定標的鑄幣，不能把每一筆兌換都當成同一個幣。")
    bars = sorted(bars, key=lambda bar: bar.open_time)
    cash = float(capital)
    base = 0.0
    rows = []
    skipped_transfers = 0
    slip = slippage_bps / 10000.0
    for event in events:
        if event.get("kind") == "transfer":
            skipped_transfers += 1
            continue
        if event.get("kind") != "swap" or not event.get("pnl_relevant"):
            continue
        when_text = event.get("time")
        if not when_text:
            rows.append({"signature": event.get("signature"), "status": "skipped", "reason_zh": "沒有成交時間"})
            continue
        when = datetime.fromisoformat(when_text)
        target = when.timestamp() + delay_seconds
        bar = next((item for item in bars if item.open_time.timestamp() >= target), None)
        if bar is None:
            rows.append({"signature": event.get("signature"), "status": "skipped", "reason_zh": "延遲之後沒有 K 線，沒有編價"})
            continue
        received_mint = event.get("received_mint")
        paid_mint = event.get("paid_mint")
        if received_mint == base_mint:
            side = "buy"
            wanted = float(event.get("received_qty") or 0.0)
        elif paid_mint == base_mint:
            side = "sell"
            wanted = float(event.get("paid_qty") or 0.0)
        else:
            rows.append({"signature": event.get("signature"), "status": "skipped", "reason_zh": "這筆兌換不是指定標的"})
            continue
        if side == "buy":
            fill = bar.open * (1.0 + slip)
            qty = min(wanted, cash / fill) if fill > 0 else 0.0
            if qty <= 0:
                rows.append({"signature": event.get("signature"), "status": "skipped", "reason_zh": "現金不足"})
                continue
            scaled = qty + 1e-12 < wanted
            cash -= qty * fill
            base += qty
            reason = "延遲後的 K 線開盤加滑點。資金不夠就縮小，沒有用槓桿補足。"
        else:
            fill = bar.open * (1.0 - slip)
            qty = min(base, wanted)
            if qty <= 0:
                rows.append({"signature": event.get("signature"), "status": "skipped", "reason_zh": "沒有庫存可賣，不借券。"})
                continue
            scaled = False
            cash += qty * fill
            base -= qty
            reason = "只賣自己跟到的庫存。"
        rows.append(
            {
                "signature": event.get("signature"),
                "status": "filled",
                "side": side,
                "time": bar.open_time.isoformat(),
                "fill_price": fill,
                "qty": qty,
                "scaled_down": scaled,
                "cash_after": cash,
                "base_after": base,
                "reason_zh": reason,
            }
        )
    equity = cash + base * (bars[-1].close if base else 0.0)
    return {
        "rows": rows,
        "cash": cash,
        "base_qty": base,
        "equity_mark": equity,
        "initial_capital": capital,
        "skipped_transfers": skipped_transfers,
        "caveats_zh": CAVEATS_ZH
        + ["跟單回測是現貨紙上重播。沒有槓桿、沒有私鑰，未平倉的市值用最後一根收盤，那不是一筆真實成交。"],
        "price_source": "next_bar_open_after_delay",
    }
