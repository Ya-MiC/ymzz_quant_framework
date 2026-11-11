"""aiogram 紙上訊號機器人。權杖只從環境變數讀，不下單。"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ymzz.errors import YmzzError
from ymzz.gmgn import GmgnExecutionStub
from ymzz.strategies import get_strategy, list_strategies

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


class MissingTokenError(YmzzError):
    error_code = "TELEGRAM_TOKEN"


def require_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise MissingTokenError(
            "請用環境變數 TELEGRAM_BOT_TOKEN 提供機器人權杖。不要寫進檔案、對話或倉庫。"
        )
    return token


def describe_signal(exposure: float, strategy_id: str, market: str, symbol: str, when: datetime | None) -> str:
    spec = get_strategy(strategy_id)
    if exposure > 1e-9:
        side = f"做多，目標曝險 {exposure:.2f}"
    elif exposure < -1e-9:
        side = f"做空，目標曝險 {exposure:.2f}"
    else:
        side = "平倉或空手"
    stamp = when.astimezone(timezone.utc).isoformat() if when else "沒有時間"
    return (
        "【紙上訊號，不會下單】\n"
        f"策略：{spec.name_zh}（{strategy_id}）\n"
        f"市場：{market} {symbol}\n"
        f"最新收盤訊號：{side}\n"
        f"訊號時間（UTC）：{stamp}\n"
        "這不是成交，也不是投資建議。GMGN 與交易所下單都沒有接上。"
    )


def help_text() -> str:
    names = "、".join(item["id"] for item in list_strategies())
    return (
        "晏銘湛箴紙上訊號機器人。只回覆策略訊號，不連錢包、不下單。\n"
        "指令：/market 市場 來源 代碼 週期\n"
        "/strategy 策略編號 參數...\n"
        "/leverage 1|1.5|2\n"
        "/signal 讀取公開行情並給出最新訊號\n"
        f"策略：{names}\n"
        "權杖只放在環境變數 TELEGRAM_BOT_TOKEN。"
    )


def create_router(sessions: dict, load_signal):
    """load_signal(session) -> (exposure, when, warnings)。網路由呼叫端負責。"""
    try:
        from aiogram import Router
        from aiogram.filters import Command
    except ImportError as exc:
        raise MissingTokenError("沒有安裝 aiogram。它是 MIT 授權的 Telegram 套件，這版不使用 python-telegram-bot。") from exc

    router = Router()

    @router.message(Command("start"))
    async def start(message):
        await message.answer(help_text())

    @router.message(Command("market"))
    async def market(message):
        parts = (message.text or "").split()
        if len(parts) < 5:
            await message.answer("用法：/market crypto_spot okx BTC/USDT 1d")
            return
        sessions[message.chat.id] = {
            **sessions.get(message.chat.id, {}),
            "market": parts[1],
            "source": parts[2],
            "symbol": parts[3],
            "timeframe": parts[4],
        }
        await message.answer("已記下市場。這只會用來算紙上訊號。")

    @router.message(Command("strategy"))
    async def strategy(message):
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("用法：/strategy rsi_three")
            return
        try:
            get_strategy(parts[1])
        except YmzzError as exc:
            await message.answer(exc.message_zh)
            return
        sessions.setdefault(message.chat.id, {})["strategy"] = parts[1]
        await message.answer(f"策略設為 {parts[1]}。參數用預設，網頁介面可以改。")

    @router.message(Command("leverage"))
    async def leverage(message):
        parts = (message.text or "").split()
        if len(parts) != 2 or parts[1] not in {"1", "1.5", "2"}:
            await message.answer("槓桿只能是 1、1.5 或 2。現貨若選大於 1，算訊號時會被拒絕。")
            return
        sessions.setdefault(message.chat.id, {})["leverage"] = float(parts[1])
        await message.answer("已記下槓桿。機器人仍然不會下單。")

    @router.message(Command("signal"))
    async def signal(message):
        session = sessions.get(message.chat.id) or {}
        if "market" not in session or "strategy" not in session:
            await message.answer("請先 /market 與 /strategy。")
            return
        try:
            exposure, when, warnings = load_signal(session)
        except YmzzError as exc:
            await message.answer(exc.message_zh)
            return
        text = describe_signal(exposure, session["strategy"], session["market"], session["symbol"], when)
        if warnings:
            text += "\n" + "\n".join(warnings[:4])
        await message.answer(text)

    return router


def main() -> None:
    token = require_token()
    from aiogram import Bot, Dispatcher
    import asyncio

    sessions: dict = {}

    def load_signal(session):
        from ymzz.data import load_market
        from ymzz.http_cache import DataClient
        from ymzz.strategies import compute_signals

        bars, _funding, _prov = load_market(session, DataClient("data/cache"))
        signals, _ind, warnings = compute_signals(session["strategy"], [bar.close for bar in bars], session.get("params"), False)
        return signals[-1] if signals else 0.0, bars[-1].close_time if bars else None, warnings

    async def run():
        bot = Bot(token)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(sessions, load_signal))
        # 明確不註冊任何下單處理器。GMGN 保持未實作。
        _ = GmgnExecutionStub()
        await dispatcher.start_polling(bot)

    asyncio.run(run())


if __name__ == "__main__":
    main()
