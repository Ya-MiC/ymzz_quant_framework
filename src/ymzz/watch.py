"""只讀的 Solana 公開地址餘額。不連 Phantom、不簽名、不收私鑰。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ymzz.errors import WalletError

RPC_URL = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")


def validate_public_address(address: str) -> str:
    text = (address or "").strip()
    if any(ch.isspace() for ch in text) or "," in text:
        raise WalletError("請只貼一個公開地址。助記詞、私鑰與一串地址都不要貼。")
    if not (32 <= len(text) <= 44) or not _BASE58.match(text):
        raise WalletError("這不像 Solana 公開地址。私鑰通常更長，本頁不接受。")
    return text


def _rpc(client, method: str, params: list) -> dict:
    payload, _prov = client.post_json(
        RPC_URL,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        mode="live",
        headers={"content-type": "application/json"},
    )
    if payload.get("error"):
        err = payload["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise WalletError(f"公共 RPC 拒絕讀取：{message}。沒有改用需要金鑰的服務。")
    return payload


def fetch_balances(address: str, client) -> dict:
    text = validate_public_address(address)
    sol = _rpc(client, "getBalance", [text])
    lamports = int((sol.get("result") or {}).get("value") or 0)
    tokens = []
    for program in TOKEN_PROGRAMS:
        payload = _rpc(
            client,
            "getTokenAccountsByOwner",
            [text, {"programId": program}, {"encoding": "jsonParsed"}],
        )
        for row in (payload.get("result") or {}).get("value") or []:
            info = (((row.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = (info.get("tokenAmount") or {})
            ui = amount.get("uiAmount")
            if ui in (None, 0):
                continue
            tokens.append(
                {
                    "mint": info.get("mint"),
                    "amount": ui,
                    "decimals": amount.get("decimals"),
                    "program": program,
                }
            )
    return {
        "address": text,
        "sol": lamports / 1_000_000_000,
        "tokens": tokens,
        "rpc": RPC_URL,
        "read_only": True,
        "note_zh": "只讀公共 RPC。沒有連接 Phantom，沒有簽名，也沒有判斷這個地址是不是聰明錢。",
    }


def wallet_file(root: Path) -> Path:
    return root / "data" / "dashboard" / "wallets.json"


def load_watchlist(root: Path) -> list[str]:
    found: list[str] = []
    env = os.environ.get("SOL_WATCH_ADDRESSES") or ""
    for piece in env.split(","):
        piece = piece.strip()
        if not piece:
            continue
        found.append(validate_public_address(piece))
    path = wallet_file(root)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        for piece in payload.get("addresses") or []:
            found.append(validate_public_address(piece))
    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def add_watch_address(root: Path, address: str) -> list[str]:
    text = validate_public_address(address)
    path = wallet_file(root)
    current = []
    if path.exists():
        current = list(json.loads(path.read_text(encoding="utf-8")).get("addresses") or [])
    if text not in current:
        current.append(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"addresses": current}, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_watchlist(root)
