"""行情数据接入层。

优先使用新浪日线接口（稳定、免 token），失败自动重试；
并支持备用东财接口。所有数据按 (代码, 区间, 复权) 维度落盘缓存，
避免重复请求、提升回测速度。本层仅服务于 A 股。
"""
import os
import time
from typing import Optional

import akshare as ak
import pandas as pd

from .config import Config


def _is_a_share(code: str) -> bool:
    """仅允许沪市(6)、深市主板/创业板(0/3)、北交所(8/4) 的 6 位 A 股代码。"""
    return len(code) == 6 and code.isdigit() and code[0] in ("6", "0", "3", "8", "4")


def assert_a_share(code: str) -> None:
    """显式校验代码为 A 股，非法则抛错（避免误接指数/基金/非 A 股接口）。"""
    if not _is_a_share(code):
        raise ValueError(
            f"仅支持 A 股（6 位代码，沪市 6 开头 / 深市 0 或 3 开头 / 北交所 8 或 4 开头），"
            f"收到：{code!r}"
        )


def _to_sina_symbol(code: str) -> str:
    """将纯数字 A 股代码转换为新浪接口所需的带市场前缀代码。"""
    code = str(code).strip()
    assert_a_share(code)
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code  # 8/4 开头


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一字段名与类型，并按日期升序排列。"""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]]
    return df


def load_daily(
    code: str,
    start: str,
    end: str,
    adjust: str = "qfq",
    cache_dir: Optional[str] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """获取单只 A 股日线行情（前复权）。

    返回列：date, open, high, low, close, volume, amount。
    """
    assert_a_share(code)
    cache_dir = cache_dir or Config().cache_dir
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{code}_{start}_{end}_{adjust}.csv")

    if use_cache and os.path.exists(cache_file):
        df = pd.read_csv(cache_file, parse_dates=["date"])
        return _normalize(df)

    df = None
    last_err: Optional[Exception] = None

    # 主源：新浪
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(
                symbol=_to_sina_symbol(code),
                start_date=start,
                end_date=end,
                adjust=adjust,
            )
            if df is not None and not df.empty:
                break
        except Exception as e:  # noqa: BLE001 - 网络抖动需重试
            last_err = e
            time.sleep(1.5)

    # 备用源：东财
    if df is None or df.empty:
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust=adjust
            )
            if df is not None and not df.empty:
                df = df.rename(
                    columns={
                        "日期": "date",
                        "开盘": "open",
                        "最高": "high",
                        "最低": "low",
                        "收盘": "close",
                        "成交量": "volume",
                        "成交额": "amount",
                    }
                )
                last_err = None
        except Exception as e:  # noqa: BLE001
            last_err = e

    if df is None or df.empty:
        raise RuntimeError(f"无法获取行情 {code}（{start}~{end}）：{last_err}")

    df = _normalize(df)
    df.to_csv(cache_file, index=False)
    return df
