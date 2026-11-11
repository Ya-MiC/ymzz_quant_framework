import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ymzz.api import create_app
from ymzz.engine import Bar
from ymzz.gmgn import GmgnExecutionStub
from ymzz.telegram_bot import MissingTokenError, describe_signal, require_token
from ymzz.wallet import parse_transaction, replay_swaps

ROOT = Path(__file__).parent / "fixtures"
UTC = timezone.utc


def _tx(owner, pre, post, fee=5000, err=None):
    keys = [{"pubkey": owner, "signer": True}]
    return {
        "result": {
            "blockTime": 1790000000,
            "meta": {
                "err": err,
                "fee": fee,
                "preBalances": [1_000_000_000],
                "postBalances": [1_000_000_000 - fee],
                "preTokenBalances": [
                    {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amount}} for mint, amount in pre
                ],
                "postTokenBalances": [
                    {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amount}} for mint, amount in post
                ],
            },
            "transaction": {"signatures": ["sig"], "message": {"accountKeys": keys}},
        }
    }


def test_swap_transfer_and_recorded_fee_payer():
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    bonk = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    swap = parse_transaction(_tx("WALLET", [(usdc, 100), (bonk, 10)], [(usdc, 50), (bonk, 20)]), "WALLET")
    assert swap["kind"] == "swap"
    assert swap["pnl_relevant"] is True
    transfer = parse_transaction(_tx("WALLET", [(usdc, 100)], [(usdc, 90)]), "WALLET")
    assert transfer["kind"] == "transfer"
    assert transfer["pnl_relevant"] is False
    failed = parse_transaction(_tx("WALLET", [(usdc, 1)], [(usdc, 1)], err={"InstructionError": []}), "WALLET")
    assert failed["kind"] == "failed"
    recorded = json.loads((ROOT / "solana_tx_recorded.json").read_text(encoding="utf-8"))
    signer = "EMCcpe4aoH2rgzUTJJVk8fnbcGBiMjeh3Y8Ynfdw2WrJ"
    pool = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"
    assert parse_transaction(recorded, signer)["kind"] == "fee_only"
    pool_event = parse_transaction(recorded, pool)
    assert pool_event["kind"] in {"swap", "swap_ambiguous"}


def test_copy_uses_delayed_bar_and_ignores_transfer():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(start, start + timedelta(hours=1), 100, 100, 100, 100, closed=True),
        Bar(start + timedelta(seconds=1000), start + timedelta(seconds=2000), 110, 110, 110, 110, closed=True),
    ]
    mint = "TOKEN"
    events = [
        {"kind": "transfer", "pnl_relevant": False, "time": start.isoformat()},
        {
            "kind": "swap",
            "pnl_relevant": True,
            "time": start.isoformat(),
            "signature": "buy",
            "received_mint": mint,
            "paid_mint": "USDC",
            "received_qty": 1,
            "paid_qty": 100,
        },
    ]
    result = replay_swaps(events, bars, delay_seconds=1000, slippage_bps=5, capital=10, base_mint=mint)
    fill = result["rows"][0]
    assert fill["side"] == "buy"
    assert fill["fill_price"] == pytest.approx(110 * 1.0005)
    assert fill["scaled_down"] is True
    assert fill["qty"] == pytest.approx(10 / fill["fill_price"])
    assert result["skipped_transfers"] == 1
    assert result["cash"] == pytest.approx(0, abs=1e-8)


def test_api_rejects_spot_leverage_and_bad_cap_before_network(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("不該為了錯誤槓桿去打行情")

    monkeypatch.setattr("ymzz.api.load_market", explode)
    client = TestClient(create_app())
    spot = client.post(
        "/api/backtest",
        json={"market": "crypto_spot", "source": "okx", "symbol": "BTC/USDT", "strategy": "rsi_single", "leverage": 2, "capital": 10},
    )
    assert spot.status_code == 400
    assert "現貨" in spot.json()["message_zh"]
    perp = client.post(
        "/api/backtest",
        json={"market": "crypto_perp", "source": "okx", "symbol": "BTC/USDT", "strategy": "macd", "leverage": 3, "capital": 10},
    )
    assert perp.status_code == 400
    stock = client.post("/api/backtest", json={"market": "stock_app", "symbol": "x", "strategy": "rsi_single", "leverage": 1})
    assert stock.status_code == 400
    assert "yfinance" in stock.json()["message_zh"]


def test_api_backtest_with_injected_bars(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    for i in range(40):
        price = 100 + (i % 7) - 3
        opened = start + timedelta(days=i)
        bars.append(Bar(opened, opened + timedelta(days=1), price, price + 2, price - 2, price, 1, 1000, True))

    def fake_load(_spec, _client, now=None):
        return bars, [], {
            "source_url": "fixture://bars",
            "sha256": "abc",
            "fetched_at_kst": "2026-11-10T19:00:00+09:00",
            "dropped_unclosed": 1,
            "source": "fixture",
        }

    monkeypatch.setattr("ymzz.api.load_market", fake_load)
    client = TestClient(create_app())
    response = client.post(
        "/api/backtest",
        json={
            "market": "crypto_spot",
            "source": "okx",
            "symbol": "BTC/USDT",
            "strategy": "rsi_single",
            "params": {"period": 6, "oversold": 30, "overbought": 70},
            "leverage": 1,
            "capital": 10,
            "fee_bps": 10,
            "slippage_bps": 5,
            "tp_pct": 0.2,
            "sl_pct": 0.2,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["live_trading"] is False
    assert len(body["equity"]) == 40
    assert "rsi" in body["indicators"]


def test_signal_text_and_secrets(monkeypatch):
    text = describe_signal(1, "rsi_three", "crypto_spot", "BTC/USDT", datetime(2026, 11, 10, tzinfo=UTC))
    assert "紙上訊號" in text
    assert "不會下單" in text
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(MissingTokenError):
        require_token()
    with pytest.raises(NotImplementedError):
        GmgnExecutionStub().submit_spot_order({"symbol": "BTC"})
    root = Path(__file__).resolve().parents[1]
    blob = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in root.joinpath("src").rglob("*.py"))
    assert "import telegram" not in blob
    assert "from telegram" not in blob
    assert "import freqtrade" not in blob
    assert "import vectorbt" not in blob
    assert "import yfinance" not in blob
    assert "yfinance" in blob
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", blob)
