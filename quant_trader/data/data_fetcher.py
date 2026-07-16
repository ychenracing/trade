"""
行情数据获取层 - 基于akshare
支持股票列表、日线行情、实时行情、基本面数据
"""
import datetime
from typing import Optional
import pandas as pd
import numpy as np

try:
    import akshare as ak
except ImportError:
    raise ImportError("请安装akshare: pip install akshare")

from utils.logger import log
from utils.helpers import format_code, is_st_stock
from config.settings import DATA_CONFIG, DATA_DIR


class DataFetcher:
    """A股行情数据获取"""

    def __init__(self):
        self.cache_dir = DATA_DIR
        self.cache_enabled = DATA_CONFIG["cache_enabled"]

    # ============ 股票列表 ============

    def get_stock_list(self, pool: str = "hs300") -> pd.DataFrame:
        """
        获取股票池
        pool: hs300=沪深300, zz500=中证500, all=全部A股
        返回: DataFrame[code, name]
        """
        try:
            if pool == "hs300":
                df = ak.index_stock_cons(symbol="000300")
                df = df[["品种代码", "品种名称"]].rename(
                    columns={"品种代码": "code", "品种名称": "name"}
                )
            elif pool == "zz500":
                df = ak.index_stock_cons(symbol="000905")
                df = df[["品种代码", "品种名称"]].rename(
                    columns={"品种代码": "code", "品种名称": "name"}
                )
            else:
                df = ak.stock_info_a_code_name()
                df = df.rename(columns={"code": "code", "name": "name"})

            df["code"] = df["code"].apply(format_code)
            log.info(f"获取股票池[{pool}]完成, 共{len(df)}只股票")
            return df
        except Exception as e:
            log.error(f"获取股票池失败: {e}")
            # 返回一个硬编码的备选列表
            return self._get_fallback_stocks()

    def _get_fallback_stocks(self) -> pd.DataFrame:
        """备选股票列表（网络异常时使用）"""
        stocks = [
            ("600519", "贵州茅台"), ("601318", "中国平安"), ("600036", "招商银行"),
            ("000858", "五粮液"), ("000333", "美的集团"), ("600276", "恒瑞医药"),
            ("601166", "兴业银行"), ("002594", "比亚迪"), ("600030", "中信证券"),
            ("601398", "工商银行"), ("600000", "浦发银行"), ("000651", "格力电器"),
            ("600887", "伊利股份"), ("601888", "中国中免"), ("002475", "立讯精密"),
            ("600031", "三一重工"), ("601012", "隆基绿能"), ("600089", "特变电工"),
            ("000725", "京东方A"), ("601628", "中国人寿"),
        ]
        return pd.DataFrame(stocks, columns=["code", "name"])

    # ============ 日线行情 ============

    def get_daily_data(
        self,
        code: str,
        start_date: str,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取日K线数据
        adjust: qfq=前复权, hfq=后复权, ""=不复权
        返回标准格式: DataFrame[date, open, high, low, close, volume, amount, turnover]
        """
        code = format_code(code)
        if end_date is None:
            end_date = datetime.date.today().strftime("%Y%m%d")

        # 检查缓存
        cache_key = f"daily_{code}_{start_date}_{end_date}_{adjust}"
        cache_file = self.cache_dir / f"{cache_key}.parquet"
        if self.cache_enabled and cache_file.exists():
            file_mtime = datetime.datetime.fromtimestamp(cache_file.stat().st_mtime)
            if datetime.datetime.now() - file_mtime < datetime.timedelta(
                hours=DATA_CONFIG["cache_expire_hours"]
            ):
                df = pd.read_parquet(cache_file)
                log.debug(f"从缓存读取{code}日线数据: {len(df)}条")
                return df

        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if df.empty:
                log.warning(f"获取{code}日线数据为空")
                return pd.DataFrame()

            # 标准化列名
            df = df.rename(columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "换手率": "turnover",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            # 确保数值类型
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            if "turnover" in df.columns:
                df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")

            # 缓存
            if self.cache_enabled:
                df.to_parquet(cache_file, index=False)

            log.debug(f"获取{code}日线数据: {len(df)}条, {df['date'].min()} ~ {df['date'].max()}")
            return df

        except Exception as e:
            log.error(f"获取{code}日线数据失败: {e}")
            # 网络失败时回退到旧缓存（即使已过期）
            if self.cache_enabled and cache_file.exists():
                log.warning(f"网络失败，使用旧缓存: {code}")
                df = pd.read_parquet(cache_file)
                return df
            return pd.DataFrame()

    def get_batch_daily(
        self,
        codes: list,
        start_date: str,
        end_date: Optional[str] = None,
    ) -> dict:
        """批量获取多只股票日线数据"""
        results = {}
        total = len(codes)
        for i, code in enumerate(codes, 1):
            code = format_code(code)
            df = self.get_daily_data(code, start_date, end_date)
            if not df.empty:
                results[code] = df
            if i % 50 == 0:
                log.info(f"批量获取进度: {i}/{total}")
        log.info(f"批量获取完成: {len(results)}/{total}")
        return results

    # ============ 实时行情 ============

    def get_realtime_quotes(self, codes: list) -> pd.DataFrame:
        """
        获取实时行情
        返回: DataFrame[code, name, price, change_pct, volume, amount, ...]
        """
        try:
            df = ak.stock_zh_a_spot_em()
            df = df.rename(columns={
                "代码": "code",
                "名称": "name",
                "最新价": "price",
                "涨跌幅": "change_pct",
                "成交量": "volume",
                "成交额": "amount",
                "今开": "open",
                "最高": "high",
                "最低": "low",
                "昨收": "pre_close",
            })
            df["code"] = df["code"].apply(format_code)
            codes = [format_code(c) for c in codes]
            df = df[df["code"].isin(codes)]
            return df
        except Exception as e:
            log.error(f"获取实时行情失败: {e}")
            return pd.DataFrame()

    # ============ 基本面数据 ============

    def get_stock_basic_info(self, code: str) -> dict:
        """获取个股基本信息（市值、PE、PB等）"""
        code = format_code(code)
        try:
            df = ak.stock_individual_info_em(symbol=code)
            info = {}
            for _, row in df.iterrows():
                info[row["item"]] = row["value"]
            return {
                "code": code,
                "name": info.get("股票简称", ""),
                "market_cap": self._parse_market_cap(info.get("总市值", "0")),
                "pe": float(info.get("市盈率(动态)", 0)) if info.get("市盈率(动态)") else 0,
                "pb": float(info.get("市净率", 0)) if info.get("市净率") else 0,
                "industry": info.get("行业", ""),
                "list_date": info.get("上市时间", ""),
            }
        except Exception as e:
            log.warning(f"获取{code}基本信息失败: {e}")
            return {}

    def _parse_market_cap(self, cap_str) -> float:
        """解析市值字符串到亿元"""
        try:
            if isinstance(cap_str, (int, float)):
                return float(cap_str)
            cap_str = str(cap_str).replace("亿", "").replace(",", "")
            return float(cap_str)
        except (ValueError, TypeError):
            return 0.0

    # ============ 股票筛选 ============

    def screen_stocks(
        self,
        codes: list,
        exclude_st: bool = True,
        min_market_cap: float = 0,
        min_avg_volume: float = 0,
    ) -> list:
        """
        初步筛选股票
        排除ST股、小市值、低成交量
        """
        try:
            # 获取全部A股实时行情用于筛选
            df = ak.stock_zh_a_spot_em()
            df = df.rename(columns={
                "代码": "code", "名称": "name",
                "成交量": "volume", "总市值": "market_cap",
            })
            df["code"] = df["code"].apply(format_code)
            codes = [format_code(c) for c in codes]
            df = df[df["code"].isin(codes)]

            # 排除ST
            if exclude_st:
                mask = ~df["name"].apply(is_st_stock)
                df = df[mask]

            # 市值过滤
            if min_market_cap > 0 and "market_cap" in df.columns:
                df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
                df = df[df["market_cap"] >= min_market_cap]

            filtered = df["code"].tolist()
            log.info(f"股票筛选: {len(codes)} -> {len(filtered)} (排除ST/小市值)")
            return filtered

        except Exception as e:
            log.warning(f"股票筛选失败，使用原始列表: {e}")
            return codes

    # ============ 交易日历 ============

    def get_trade_calendar(self, start: str, end: str) -> list:
        """获取交易日历"""
        try:
            df = ak.tool_trade_date_hist_sina()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            mask = (df["trade_date"] >= pd.to_datetime(start).date()) & \
                   (df["trade_date"] <= pd.to_datetime(end).date())
            return df.loc[mask, "trade_date"].tolist()
        except Exception as e:
            log.warning(f"获取交易日历失败: {e}")
            # 回退到工作日
            dates = pd.date_range(start, end, freq="B")
            return [d.date() for d in dates]
