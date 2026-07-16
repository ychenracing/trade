"""
实盘交易执行器 - QMT接口
⚠️ 实盘交易有风险，使用前请充分测试
目前为接口占位，需安装xtquant后启用
"""
from typing import Optional
from execution.base import BaseExecutor, Order, OrderSide, OrderStatus, AccountInfo
from utils.logger import log
from utils.helpers import format_code, round_lot


class QMTExecutor(BaseExecutor):
    """
    QMT交易执行器
    需安装xtquant: pip install xtquant
    需本地运行QMT客户端并开启交易接口
    """

    def __init__(self, account_id: str, path: str = ""):
        self.account_id = account_id
        self.path = path
        self.connected = False
        self._xt_trader = None
        self._connect()

    def _connect(self):
        """连接QMT"""
        try:
            from xtquant import xttrader, xtdata
            from config.settings import QMT_CONFIG
            path = self.path or QMT_CONFIG["default_path"]
            session_id = int(self.account_id[-6:])
            self._xt_trader = xttrader.XtQuantTrader(path, session_id)
            self._xt_trader.start()
            connect_result = self._xt_trader.connect()
            if connect_result == 0:
                self._xt_trader.subscribe(self.account_id)
                self.connected = True
                log.info(f"QMT连接成功, 账户={self.account_id}")
            else:
                log.error(f"QMT连接失败, 返回码={connect_result}")
        except ImportError:
            log.warning("xtquant未安装, 实盘功能不可用。pip install xtquant")
        except Exception as e:
            log.error(f"QMT连接异常: {e}")

    def buy(self, code: str, name: str, price: float, shares: int) -> Order:
        """买入"""
        code = format_code(code)
        shares = round_lot(shares)
        if not self.connected or shares <= 0:
            return Order(code, name, OrderSide.BUY, price, shares, OrderStatus.REJECTED,
                        error_msg="未连接或数量无效")

        try:
            from xtquant import xttrader
            # 沪市0, 深市1
            market = "SH" if code.startswith("6") else "SZ"
            stock_code = f"{code}.{market}"

            order_id = self._xt_trader.order_stock(
                self.account_id, stock_code,
                xttrader.STOCK_BUY, shares,
                xttrader.FIX_PRICE, price,
            )
            log.info(f"QMT买入下单: {name}({code}) {shares}股 @ {price:.2f}, order_id={order_id}")
            return Order(
                code=code, name=name, side=OrderSide.BUY,
                price=price, shares=shares,
                status=OrderStatus.PENDING,
                order_id=str(order_id),
            )
        except Exception as e:
            log.error(f"QMT买入失败: {e}")
            return Order(code, name, OrderSide.BUY, price, shares, OrderStatus.REJECTED,
                        error_msg=str(e))

    def sell(self, code: str, name: str, price: float, shares: int) -> Order:
        """卖出"""
        code = format_code(code)
        shares = round_lot(shares)
        if not self.connected or shares <= 0:
            return Order(code, name, OrderSide.SELL, price, shares, OrderStatus.REJECTED,
                        error_msg="未连接或数量无效")

        try:
            from xtquant import xttrader
            market = "SH" if code.startswith("6") else "SZ"
            stock_code = f"{code}.{market}"

            order_id = self._xt_trader.order_stock(
                self.account_id, stock_code,
                xttrader.STOCK_SELL, shares,
                xttrader.FIX_PRICE, price,
            )
            log.info(f"QMT卖出下单: {name}({code}) {shares}股 @ {price:.2f}, order_id={order_id}")
            return Order(
                code=code, name=name, side=OrderSide.SELL,
                price=price, shares=shares,
                status=OrderStatus.PENDING,
                order_id=str(order_id),
            )
        except Exception as e:
            log.error(f"QMT卖出失败: {e}")
            return Order(code, name, OrderSide.SELL, price, shares, OrderStatus.REJECTED,
                        error_msg=str(e))

    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        if not self.connected:
            return AccountInfo(cash=0, total_value=0, positions={}, today_trades=[])

        try:
            from xtquant import xtdata
            asset = self._xt_trader.query_stock_asset(self.account_id)
            if asset is None:
                return AccountInfo(cash=0, total_value=0, positions={}, today_trades=[])

            positions = {}
            pos_list = self._xt_trader.query_stock_positions(self.account_id)
            if pos_list:
                for pos in pos_list:
                    code = pos.stock_code.split(".")[0]
                    from risk.position_sizer import PositionInfo
                    positions[code] = PositionInfo(
                        code=code,
                        name="",
                        shares=pos.volume,
                        cost_price=pos.open_price,
                        current_price=pos.current_price,
                        market_value=pos.market_value,
                        profit_loss=pos.market_value - pos.open_price * pos.volume,
                        profit_pct=(pos.current_price - pos.open_price) / pos.open_price if pos.open_price > 0 else 0,
                        peak_price=max(pos.current_price, pos.open_price),
                        trailing_active=pos.current_price > pos.open_price,
                        base_shares=pos.volume,
                        entry_atr=0.0,
                        price_gain_since_entry=(pos.current_price - pos.open_price) / pos.open_price if pos.open_price > 0 else 0.0,
                    )

            return AccountInfo(
                cash=asset.cash,
                total_value=asset.total_asset,
                positions=positions,
                today_trades=[],
            )
        except Exception as e:
            log.error(f"QMT查询账户失败: {e}")
            return AccountInfo(cash=0, total_value=0, positions={}, today_trades=[])

    def get_positions(self) -> dict:
        """获取持仓"""
        return self.get_account().positions

    def disconnect(self):
        """断开连接"""
        if self._xt_trader:
            self._xt_trader.stop()
            self.connected = False
            log.info("QMT连接已断开")
