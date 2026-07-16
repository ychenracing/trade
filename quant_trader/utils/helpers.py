"""
工具函数 - 交易日历、A股交易规则、辅助计算
"""
import datetime
from typing import Optional
import pandas as pd
import numpy as np

from utils.logger import log


# ============ 交易日历 ============

# 交易日历缓存
_TRADE_DATES_CACHE = None
_TRADE_DATES_LOADED = False

def _load_trade_dates():
    """加载交易日历到缓存（失败时不缓存None，允许下次重试）"""
    global _TRADE_DATES_CACHE, _TRADE_DATES_LOADED
    if _TRADE_DATES_LOADED and _TRADE_DATES_CACHE is not None:
        return _TRADE_DATES_CACHE
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        _TRADE_DATES_CACHE = set(pd.to_datetime(df["trade_date"]).dt.date)
        _TRADE_DATES_LOADED = True
    except Exception:
        _TRADE_DATES_CACHE = None
        _TRADE_DATES_LOADED = False  # 失败时不设flag，允许下次重试
    return _TRADE_DATES_CACHE


def is_trading_day(date: Optional[datetime.date] = None) -> bool:
    """
    判断是否为交易日
    优先使用akshare交易日历（含节假日，缓存避免重复请求），网络不可用时退化为周末判断
    """
    if date is None:
        date = datetime.date.today()
    # 周末不是交易日
    if date.weekday() >= 5:
        return False
    # 查缓存中的交易日历
    trade_dates = _load_trade_dates()
    if trade_dates is not None:
        return date in trade_dates
    # 网络不可用或akshare未安装时，退化为简单周末判断
    return True


def get_trading_days(start: str, end: str) -> list:
    """获取区间内的交易日列表"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        mask = (df["trade_date"] >= pd.to_datetime(start).date()) & \
               (df["trade_date"] <= pd.to_datetime(end).date())
        return df.loc[mask, "trade_date"].tolist()
    except Exception as e:
        log.warning(f"获取交易日历失败，使用简单规则: {e}")
        dates = pd.date_range(start, end, freq="B")
        return [d.date() for d in dates]


def is_market_open() -> bool:
    """判断当前是否在交易时间内"""
    now = datetime.datetime.now()
    if not is_trading_day(now.date()):
        return False
    current_time = now.time()
    # 上午 9:30-11:30
    if datetime.time(9, 30) <= current_time <= datetime.time(11, 30):
        return True
    # 下午 13:00-15:00
    if datetime.time(13, 0) <= current_time <= datetime.time(15, 0):
        return True
    return False


# ============ A股交易规则 ============

def round_lot(shares: int, lot_size: int = 100) -> int:
    """按手数取整（A股100股一手）"""
    return (shares // lot_size) * lot_size


def calc_price_limit(price: float, board: str = "main") -> tuple:
    """
    计算涨跌停价
    board: main=主板(10%), star=科创板(20%), gem=创业板(20%), st=ST(5%)
    返回: (涨停价, 跌停价)
    """
    limit_ratio = {
        "main": 0.10,
        "star": 0.20,
        "gem": 0.20,
        "st": 0.05,
    }.get(board, 0.10)

    # A股涨跌停价按四舍五入到分
    up = round(price * (1 + limit_ratio), 2)
    down = round(price * (1 - limit_ratio), 2)
    return up, down


def get_stock_board(code: str) -> str:
    """根据股票代码判断板块"""
    code = code.strip()
    if code.startswith("688"):
        return "star"       # 科创板
    elif code.startswith("300") or code.startswith("301"):
        return "gem"        # 创业板
    elif code.startswith("60") or code.startswith("00"):
        return "main"       # 主板
    return "main"


def is_st_stock(name: str) -> bool:
    """判断是否为ST股（含*ST）"""
    return "ST" in name.upper()


def format_code(code: str) -> str:
    """补全股票代码到6位"""
    return code.strip().zfill(6)


def code_with_suffix(code: str) -> str:
    """添加交易所后缀（akshare格式）"""
    code = format_code(code)
    if code.startswith("6"):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"


# ============ 费用计算 ============

def calc_commission(amount: float, rate: float = 0.0003, min_fee: float = 5.0) -> float:
    """计算佣金"""
    fee = amount * rate
    return max(fee, min_fee)


def calc_stamp_tax(amount: float, is_sell: bool = True) -> float:
    """印花税（仅卖出时收取）"""
    if is_sell:
        return amount * 0.001
    return 0.0


def calc_transfer_fee(amount: float) -> float:
    """过户费（沪深都收）"""
    return amount * 0.00002


def calc_total_cost(amount: float, is_sell: bool = True) -> float:
    """计算交易总成本"""
    commission = calc_commission(amount)
    stamp_tax = calc_stamp_tax(amount, is_sell)
    transfer = calc_transfer_fee(amount)
    return commission + stamp_tax + transfer


# ============ 技术指标 ============

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均"""
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    """简单移动平均"""
    return series.rolling(window=period).mean()


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    """ATR（平均真实波幅）"""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    MACD指标
    返回: (dif, dea, hist)
    """
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    hist = (dif - dea) * 2    # MACD柱状图
    return dif, dea, hist


def calc_donchian(high: pd.Series, low: pd.Series, period: int) -> tuple:
    """
    唐奇安通道
    返回: (upper, lower, middle)
    """
    upper = high.rolling(window=period).max().shift(1)   # 过去N日最高价
    lower = low.rolling(window=period).min().shift(1)    # 过去N日最低价
    middle = (upper + lower) / 2
    return upper, lower, middle


def calc_momentum_score(close: pd.Series, period: int = 25) -> tuple:
    """
    动量评分：加权对数回归斜率 × 年化 × R²
    
    来自社区ETF轮动策略标准方法（9db竞技场/BigQuant）
    - 斜率 > 0：上涨动量；< 0：下跌动量
    - R²越高趋势越平滑（过滤波动大的假趋势）
    - 加权回归：近期权重更高，对最新走势更敏感
    
    Args:
        close: 收盘价序列
        period: 回归窗口，默认25个交易日
    
    Returns:
        (momentum_score, r_squared)
        - momentum_score: 年化斜率 × R²，典型范围 -3 ~ +3
        - r_squared: 0~1，趋势拟合优度
    """
    if len(close) < period:
        return 0.0, 0.0

    prices = close.iloc[-period:].values.astype(float)
    log_prices = np.log(prices)

    # 指数衰减权重：近期权重更高
    x = np.arange(period, dtype=float)
    weights = np.exp((x - period + 1) / period * 2)

    # 加权最小二乘
    w_sum = np.sum(weights)
    wx = np.sum(weights * x)
    wy = np.sum(weights * log_prices)
    wxx = np.sum(weights * x * x)
    wxy = np.sum(weights * x * log_prices)

    denom = w_sum * wxx - wx * wx
    if abs(denom) < 1e-10:
        return 0.0, 0.0

    slope = (w_sum * wxy - wx * wy) / denom
    intercept = (wy - slope * wx) / w_sum

    # R²计算
    y_pred = intercept + slope * x
    y_wmean = wy / w_sum
    ss_res = np.sum(weights * (log_prices - y_pred) ** 2)
    ss_tot = np.sum(weights * (log_prices - y_wmean) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    # 年化斜率 × R²
    annualized_slope = slope * 250
    momentum_score = annualized_slope * r_squared

    return momentum_score, r_squared


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI相对强弱指标"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_position_value(price: float, capital: float, max_ratio: float) -> int:
    """
    根据资金和最大仓位比例计算可买股数
    返回取整到100股的股数
    """
    max_amount = capital * max_ratio
    shares = int(max_amount / price)
    return round_lot(shares)


def safe_divide(a: float, b: float, default: float = 0.0) -> float:
    """安全除法"""
    if b == 0:
        return default
    return a / b


def pct_change(current: float, base: float) -> float:
    """百分比变化"""
    return safe_divide(current - base, base)


def calc_trend_mode(df: pd.DataFrame) -> str:
    """
    计算趋势环境: "up" / "down" / "mixed"
    基于 MA20 vs MA60 + 价格 vs MA60
    所有调用方统一使用此函数，避免4份代码各自计算不一致
    """
    close = df["close"]
    current_price = close.iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else current_price
    ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else current_price
    if np.isnan(ma20) or np.isnan(ma60):
        return "up"
    price_above = current_price > ma60
    ma20_above = ma20 > ma60
    if price_above and ma20_above:
        return "up"
    elif not price_above and not ma20_above:
        return "down"
    else:
        return "mixed"


def find_duplicate_logic(file_paths: list) -> dict:
    """
    跨文件重复逻辑扫描：找出多份代码中相同模式的逻辑块
    审查代码时必须调用此函数，避免"同一套逻辑写多份"的铁律1违规
    
    用法: find_duplicate_logic(['main.py', 'backtest/engine.py', 'backtest_v2.py', 'backtest_300308.py'])
    返回: {模式: [文件名:行号]} 的重复映射
    """
    import re
    patterns = [
        (r'trend_mode\s*=\s*"up"', 'trend_mode手动计算'),
        (r'pos\.shares\s*\+=\s*add_shares', '加仓后手动更新shares（应由simulator处理）'),
        (r'order\.filled_price\s*\*\s*order\.filled_shares\s*-\s*pos\.cost_price', 'PnL手动计算（应用order.realized_pnl）'),
        (r'pos\.profit_loss', '引用pos.profit_loss（sell后已被修改，应用order.realized_pnl）'),
        (r'ma20\s*=.*calc_sma.*20', 'MA20手动计算（应用calc_trend_mode）'),
        (r'current_price\s*>\s*cur_ma60\s*and\s*cur_ma20\s*>\s*cur_ma60', 'trend_mode内联判断'),
    ]
    duplicates = {}
    for f in file_paths:
        try:
            with open(f) as fh:
                lines = fh.readlines()
            for i, line in enumerate(lines):
                for pat, desc in patterns:
                    if re.search(pat, line):
                        duplicates.setdefault(desc, []).append(f"{f}:{i+1}")
        except FileNotFoundError:
            pass
    # 只返回有重复的
    return {k: v for k, v in duplicates.items() if len(v) > 0}
