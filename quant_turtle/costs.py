"""交易成本计算（单一真相源）。

佣金、滑点、印花税的计算逻辑在组合层（Portfolio）与模拟券商（PaperBroker）两处都会用到，
抽到此处统一实现，避免某处改了费率口径而另一处遗漏（曾导致回测与模拟撮合不一致）。
"""
from typing import Tuple


def effective_price(side: str, price: float, slippage: float) -> float:
    """考虑滑点后的实际成交价。买入加滑点、卖出减滑点。"""
    if side == "BUY":
        return price * (1.0 + slippage)
    return price * (1.0 - slippage)


def trade_value(
    side: str,
    shares: int,
    price: float,
    slippage: float,
    commission: float,
    stamp_duty: float,
) -> Tuple[float, float]:
    """返回（实际成交价, 现金变动额）。

    BUY：现金减少 = 股数 × 含滑点价 ×(1+佣金)
    SELL：现金增加 = 股数 × 含滑点价 ×(1-佣金-印花税)
    """
    eff = effective_price(side, price, slippage)
    if side == "BUY":
        cash_delta = -(shares * eff * (1.0 + commission))
    else:
        cash_delta = shares * eff * (1.0 - commission - stamp_duty)
    return eff, cash_delta
