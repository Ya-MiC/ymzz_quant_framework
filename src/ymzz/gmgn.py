"""GMGN 現貨執行的未來介面。這版不連線、不下單。"""

from __future__ import annotations


class GmgnExecutionStub:
    """若將來接上，也只會是現貨兌換，不能把現貨變成永續槓桿。"""

    name = "gmgn"
    live_trading = False

    def submit_spot_order(self, order: dict) -> dict:
        raise NotImplementedError(
            "GMGN 執行還沒有接上。本框架禁止下單。將來若接的是現貨兌換，"
            "選單上的槓桿也不會變成鏈上保證金。收到的委託已忽略："
            + str(sorted(order.keys()))
        )
