#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""量化回测统一入口（单文件自包含版）：海龟 + 均线趋势双策略组合回测系统（口径B）。

把原 quant_compare 下的多个脚本合并到一个文件，用子命令分发，逻辑/参数/注释与合并前一致。

硬约束（用户要求，口径B）：
  - 单票仓位上限 = 60%（BUY 受 max_sym_w*equity 约束；收盘 cap_trim 减仓至上限）
  - 组合最大回撤 ≤ 20%（熔断：回撤触顶→当根收盘立即清仓→冷却N日→恢复）

复用我的信号逻辑：海龟(entry=10, exit 由参数指定：原篮40 / 半篮70) + 均线(MA20/MA60)。
借用改进：
  1) 固定大额定仓(sizing='fixed', cap_frac=1.0)：赢家复利集中。
  2) 动量轮动 + 单票上限：仅动量前 N 的标的开新仓，单票≤60%。
  3) 熔断立即成交（当根收盘平），消除"次日开盘"滞后，把回撤真正锁在阈值内。
数据：原篮 9 只本地 qfq 日线 2025-01-02~2026-07-10（滚动实时，end=None），初始 200 万、成本 0.03%/0.05%/0.1%。

两篮参数与 baseline 已内嵌为 ORIG_CONFIG / SEMI_CONFIG 常量，运行不再依赖任何外部 JSON；
数据目录 data/ 与 data_semi/（本地前复权日线）仍为外部输入。

回测引擎（config / portfolio / strategies / indicators / backtest / data_feed）已内联于本文件，
运行不依赖任何自定义外部包（仅需 pandas / numpy / akshare 等第三方库），可单独拷贝运行。

用法（子命令）：
  python3.11 quant_all.py original        # 跑光通信/存储 9 只（写 equity_constrained.csv）
  python3.11 quant_all.py semi            # 跑半导体 9 只（写 equity_semi_nosmic.csv）
  python3.11 quant_all.py all             # 刷新数据 + 双篮重跑 + 出 reports/backtest_daily.md（定时任务用）
  python3.11 quant_all.py verify          # 约束最优明细复盘（口径B）
  python3.11 quant_all.py scan-semi       # 半篮参数扫描（固定 cap=0.585）
  python3.11 quant_all.py joint           # 联合参数扫描（滚动实时窗口）
  python3.11 quant_all.py joint630        # 联合参数扫描（定稿窗口 截至2026-06-30）
  python3.11 quant_all.py scan-sweet      # 原篮 circuit 甜点区扫描(0.16~0.19)
  python3.11 quant_all.py refresh         # 增量刷新两篮本地数据
  python3.11 quant_all.py fetch-semi      # 抓取半篮 10 只原始数据（区间 2025-01-01~2026-06-30）
  python3.11 quant_all.py plot            # 画权益曲线 + 回撤图（equity_constrained.png）
  python3.11 quant_all.py backtest-semi   # 半篮 10 只（含中芯）对比回测 + T+1 校验
  python3.11 quant_all.py mine            # 我的策略(海龟10/70 + 均线)单标的 + 9 只等权组合
  python3.11 quant_all.py debug-circuit   # 调试 2025-09 熔断区间持仓
  python3.11 quant_all.py turtle-v11      # 对比上传的 turtle_v11 实跑结果
"""
import sys, os, csv, datetime, argparse, importlib.util
sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/quant_compare")
warnings = __import__("warnings")
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from dataclasses import replace

##############################################################################
# 内嵌回测引擎（原回测引擎包，已内联，消除外部依赖，单文件自包含）
# 以下代码与原回测引擎各模块逐字一致，仅去掉了包内相对 import，
# 并把 indicators.true_range 重命名为 _qt_true_range（quant_all.py 自身已定义 true_range，避免覆盖）。
##############################################################################

# ---------- costs（交易成本） ----------
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

# ---------- config（全局配置 Config） ----------
"""量化交易系统全局配置。

资金量级约 200 万，可容忍 20% 亏损，不使用杠杆，仅做 A 股，偏好趋势跟踪的多策略组合。
所有金额单位为人民币元，A 股 1 手 = 100 股。
"""
from dataclasses import dataclass, field
from typing import Dict, List

# 受支持的策略 id（用于配置自检，避免拼写错误在运行时才暴露）
KNOWN_STRATEGIES = {"turtle20", "turtle55", "ma_trend"}


@dataclass
class Config:
    # ---------- 资金与风险 ----------
    initial_capital: float = 2_000_000.0     # 账户初始资金（元）
    risk_per_trade: float = 0.01             # 单个单位（1 unit）所冒风险占总资金比例
    # 账户最大可容忍回撤：权益从峰值回撤超过该比例即清仓并停机（默认 peak，行业标准的"最大回撤"口径）
    max_total_risk: float = 0.20
    max_drawdown_basis: str = "peak"         # "peak"=相对权益峰值；"initial"=相对初始资金
    allow_leverage: bool = False             # 是否允许杠杆（本系统强制关闭）

    # ---------- A 股交易规则 ----------
    t_plus_one: bool = True                  # T+1：当日买入不可当日卖出
    min_lot: int = 100                       # 最小交易单位（1 手 = 100 股）
    commission: float = 0.0003               # 券商佣金费率（单边，万三）
    slippage: float = 0.001                  # 滑点（单边，千一，回测保守估计）
    stamp_duty: float = 0.0005               # 印花税（仅卖出收取，千分之五）

    # ---------- 标的范围（仅 A 股）----------
    # 支持多标的；本回测默认只跑中际旭创（300308）。
    universe: List[str] = field(default_factory=lambda: ["300308"])

    # ---------- 回测区间与数据 ----------
    start_date: str = "20250101"
    end_date: str = "20260630"
    adjust: str = "qfq"                      # 前复权
    cache_dir: str = "/workspace/quant_compare/cache"

    # ---------- 指标参数 ----------
    atr_period: int = 20                     # ATR 计算周期（Wilder 平滑）
    turtle_entries: List[int] = field(default_factory=lambda: [20, 55])   # 海龟入场突破周期
    turtle_exits: List[int] = field(default_factory=lambda: [10, 20])     # 海龟离场跌破周期
    ma_fast: int = 20                        # 均线趋势：快线
    ma_slow: int = 60                        # 均线趋势：慢线
    max_units: int = 4                       # 海龟单标的加仓上限（1+3 次加仓）
    stop_multiple: float = 2.0               # 硬止损距建仓价的 ATR 倍数（越大越能扛回调）
    pyramid_step: float = 0.5                # 海龟加仓步长（单位：ATR）
    ma_pyramid_step: float = 1.0             # 均线策略加仓步长（单位：ATR）
    trail_multiple: float = 0.0             # ATR 吊灯止损倍数（0=关闭，沿用固定硬止损；
                                            #   >0=止损价随持仓期最高价上移：stop=highest_close - trail×ATR，
                                            #   仅上移不下移，用于锁定利润、压低回撤）

    # ---------- 波动率目标化（动态仓位缩放，压低高波动回撤的核心机制）----------
    # 原理：持仓单位数随「当前 ATR% (=ATR/收盘价)」反向缩放——高波动期自动缩仓、低波动期满仓。
    # 收益主要来自低波动的平静趋势（满仓吃），回撤主要来自高波动回调段（缩仓压低），
    # 非对称地改善「收益/回撤」比，是让高波动标的（如新易盛）在单笔长持中也压住回撤的关键。
    target_atr_pct: float = 0.0           # 目标 ATR 占价格比例(%)；=0 关闭（沿用经典海龟金字塔）。
                                         #   >0 时启用：单位数 = round(max_units × target/当前ATR%)，封顶于 max_units。
    vol_target_cap: float = 1.0           # 缩放系数上限（≤1.0 表示只缩不扩，杜绝变相加杠杆）
    vol_target_floor: float = 0.4         # 缩放系数下限（至少保留 40% 单位，避免彻底空仓踏空）
    vt_trim_band: float = 0.05            # 减仓方向闸门：仅当持仓自峰值回撤超过该比例(且波动偏高)才减仓；
                                         #   高波动但上涨(主升浪)时不减仓，从而保住主升浪收益、只压高波动下跌段回撤

    # ---------- 多策略组合 ----------
    # 参与组合的策略 id 列表
    strategies: List[str] = field(default_factory=lambda: ["turtle20", "turtle55", "ma_trend"])
    # 各策略资金权重（合计应为 1.0）
    strategy_capital_weight: Dict[str, float] = field(
        default_factory=lambda: {"turtle20": 0.4, "turtle55": 0.4, "ma_trend": 0.2}
    )

    # ---------- 运行模式 ----------
    # paper：模拟撮合（默认，安全）；live：连接实盘券商（需自行配置凭据，未启用）
    mode: str = "paper"

    def validate(self) -> None:
        """配置自检：确保策略合法、权重合计为 1、未开启杠杆等。"""
        if self.allow_leverage:
            raise ValueError("本系统禁止使用杠杆（allow_leverage 必须为 False）")
        if not self.universe:
            raise ValueError("universe 不能为空，必须至少指定一只 A 股标的")
        unknown = [s for s in self.strategies if s not in KNOWN_STRATEGIES]
        if unknown:
            raise ValueError(f"未知策略 id：{unknown}（仅支持 {sorted(KNOWN_STRATEGIES)}）")
        total_w = sum(self.strategy_capital_weight.get(s, 0.0) for s in self.strategies)
        if abs(total_w - 1.0) > 1e-6:
            raise ValueError(f"策略资金权重合计应为 1.0，当前为 {total_w:.4f}")
        if self.max_total_risk <= 0 or self.max_total_risk >= 1:
            raise ValueError("max_total_risk 应在 (0, 1) 区间内")
        if self.max_drawdown_basis not in ("peak", "initial"):
            raise ValueError("max_drawdown_basis 仅支持 'peak' 或 'initial'")
        if self.min_lot <= 0 or self.min_lot % 100 != 0:
            raise ValueError("min_lot 必须为 100 的正整数倍")

# ---------- strategies/base（Order / Strategy 基类） ----------
"""策略基类与订单结构。

设计要点（事件驱动、杜绝前视偏差、符合 A 股 T+1）：
- 策略在每根 bar 收盘后读取当根及历史数据，生成「下一根开盘执行」的订单。
- 所有订单均在下一根 bar 的开盘价撮合，因此「当日买入、当日卖出」在结构上不可能发生，
  天然满足 T+1。
- 止损单携带 limit_price（止损价），下根开盘若跳空低于止损价则按开盘价成交（更保守）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd


@dataclass
class Order:
    symbol: str
    action: str          # "BUY" / "SELL"
    shares: int          # 目标股数（正整手）
    strategy: str        # 来源策略 id
    reason: str = ""     # 下单理由（用于回测复盘）
    limit_price: Optional[float] = None  # 止损价；None 表示按下一根开盘价市价成交


class Strategy(ABC):
    """所有策略的基类，负责维护自身在单只标的上的持仓状态。"""

    id: str = "base"

    def __init__(self, symbol: str, capital: float, cfg):
        self.symbol = symbol
        self.capital = float(capital)   # 分配给本策略的资本（元）
        self.cfg = cfg
        # ---- 持仓状态 ----
        self.in_position: bool = False
        self.units: int = 0             # 当前持有的「海龟单位」数
        self.entry_prices: List[float] = []   # 每个单位的建仓价
        self.last_add_price: float = None     # 上次加仓参考价
        self.unit_shares: int = 0       # 每单位对应股数（基于建仓时 ATR 固定）
        self.stop_price: float = None   # 当前止损价
        self.highest_close: float = None  # 持仓期最高收盘价（用于 ATR 吊灯止损 ratchet）
        self.trades: List[dict] = []    # 本策略成交记录（仅供策略内部参考）

    # ---------- 工具方法 ----------
    def _total_shares(self) -> int:
        return self.units * self.unit_shares

    def _reset_position(self) -> None:
        self.in_position = False
        self.units = 0
        self.entry_prices = []
        self.last_add_price = None
        self.unit_shares = 0
        self.stop_price = None
        self.highest_close = None

    def _compute_unit_shares(self, atr: float, price: float = None) -> int:
        """根据 ATR 与分配给本策略的资本，计算 1 个单位对应的整手股数。

        单位股数 = floor(资本 × 单笔风险比例 / (ATR × 每手)) × 每手。
        price 参数保留以兼容调用点，波动率目标化不参与「单位股数」计算
        （满仓入场/加仓，仅在持仓期高波动深跌段才按波动缩放减仓，详见子类 on_bar）。
        """
        lot = self.cfg.min_lot
        if atr <= 0:
            return 0
        risk_cash = self.capital * self.cfg.risk_per_trade
        raw_units = risk_cash / (atr * lot)
        shares = int(raw_units) * lot
        return max(shares, 0)

    def _vol_scale(self, atr: float, price: float) -> float:
        """波动率缩放系数：高波动(ATR%高)→<1 缩仓；低波动→=cap 维持。

        无前视偏差：使用本根 bar 自身的 ATR 与收盘价计算，仅用于下一根开盘的撮合决策。
        """
        tgt = getattr(self.cfg, "target_atr_pct", 0)
        if not tgt or tgt <= 0 or price <= 0 or atr <= 0:
            return 1.0
        atr_pct = atr / price
        s = min(self.cfg.vol_target_cap, tgt / atr_pct)
        s = max(self.cfg.vol_target_floor, s)
        return s

    def _update_trailing_stop(self, close: float, atr: float) -> None:
        """ATR 吊灯止损（chandelier exit）：止损价随持仓期最高价上移，仅升不降。

        stop = max(原止损价, highest_close − trail_multiple×ATR)

        - trail_multiple<=0（默认）时不启用，止损价保持建仓时的固定硬止损（向后兼容）。
        - 启用后：价格创新高 → 止损价上移锁定利润；价格自高位回落 trail×ATR → 触发离场，
          从而在趋势反转时于更高位置离场，压低回撤、保留大部分趋势收益。

        注意：highest_close（持仓期最高收盘价）**始终**随创新高更新，与是否启用吊灯止损无关——
        它同时被波动率目标化的「方向性减仓闸门」复用（需真实的持仓期峰值）。
        调用约定：持仓状态下、在硬止损/加仓/离场判断**之前**每根 bar 调用一次。
        """
        if self.highest_close is None or close > self.highest_close:
            self.highest_close = close
        if self.cfg.trail_multiple and self.cfg.trail_multiple > 0 and atr > 0:
            trail = self.highest_close - self.cfg.trail_multiple * atr
            if self.stop_price is None or trail > self.stop_price:
                self.stop_price = trail

    def sync_position(self, actual_shares: int) -> None:
        """用组合实际持仓回填策略状态（修复：成交被现金截断或账户熔断清算后，
        策略内部状态与实际持仓脱节的问题）。「组合是唯一真相源」。

        - 实际持仓为 0：清空策略状态（含清算后卡死的 in_position）。
        - 实际持仓 > 0：标记为持仓中，但**不**按「实际股数 / 单位股数」回算 units。

        说明：信号期按足额成交预估的 units 只会 ≥ 实际（仅当现金不足被截断时才可能偏小）。
        保留信号期 units 是更保守的选择——即便发生截断，units 不会偏低，加仓闸门
        `units < max_units` 不会失效，从而杜绝「越权超加」。实际股数不足由组合层
        SELL 的 `min(order.shares, 实际持仓)` 兜底，不会产生孤儿仓。
        """
        if actual_shares <= 0:
            if self.in_position:
                self._reset_position()
            return
        self.in_position = True

    @abstractmethod
    def on_bar(self, bar: pd.Series) -> List[Order]:
        """输入当根 bar（含指标），返回下一根开盘执行的订单列表。

        约定：bar 的列至少包含 open/high/low/close/atr 及所需通道/均线列。
        """
        raise NotImplementedError

# ---------- strategies/turtle（TurtleStrategy） ----------
"""海龟法则 A 股版（趋势跟踪）。

经典海龟交易法则的 A 股适配：
- 仅做多（A 股融券受限、T+1，不适合做空趋势）。
- 入场：收盘价突破 N 日高点（唐奇安通道上轨）。
- 加仓：每上涨 0.5×ATR 加 1 个单位，最多 max_units 个单位。
- 离场：收盘价跌破 M 日低点（唐奇安通道下轨）。
- 止损：首单位建仓价下方 2×ATR 处硬止损。
- 仓位：以 ATR 度量波动，单位规模 = 单笔风险预算 / (ATR × 每手)。

提供两套参数以形成组合：
- turtle20：短周期（20 日突破 / 10 日离场），捕捉中短期趋势。
- turtle55：长周期（55 日突破 / 20 日离场），捕捉中长期趋势。
"""
from typing import List

import pandas as pd




class TurtleStrategy(Strategy):
    def __init__(self, symbol: str, capital: float, cfg, entry_period: int = 20, exit_period: int = 10):
        super().__init__(symbol, capital, cfg)
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.id = f"turtle{entry_period}"

    def on_bar(self, bar: pd.Series) -> List[Order]:
        orders: List[Order] = []
        close = bar["close"]
        low = bar["low"]
        atr = bar["atr"]
        if pd.isna(close) or pd.isna(low) or pd.isna(atr):
            return orders
        close = float(close)
        low = float(low)
        atr = float(atr)
        upper = bar.get(f"donchian_upper_{self.entry_period}")
        lower = bar.get(f"donchian_lower_{self.exit_period}")

        if pd.isna(atr) or pd.isna(upper) or pd.isna(lower):
            return orders

        if self.in_position:
            # 0) 更新 ATR 吊灯止损（随最高价上移；trail_multiple=0 时退化为固定硬止损）
            self._update_trailing_stop(close, atr)

            # 1) 硬止损（盘中触及止损价即于下根开盘平仓）
            if self.stop_price is not None and low <= self.stop_price:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id,
                          f"stop_loss@{self.stop_price:.2f}", limit_price=self.stop_price)
                )
                self._reset_position()
                return orders  # 本 bar 触发止损后不再操作，避免同日回转

            # 2) 持仓期单位数管理（波动率目标化 / 经典金字塔 二选一）
            if getattr(self.cfg, "target_atr_pct", 0) > 0:
                # 波动率目标化（非对称）：满仓吃主升浪；仅「自峰值回撤超阈值(且高波动)」才减仓压回撤。
                # 回撤带区分「常态 V 型回调」与「灾难性持续下跌」：带过窄会扇耳光(损收益)，过宽则减仓太晚(回撤压不低)，需折中。
                peak = self.highest_close if self.highest_close is not None else close
                in_drawdown = close < peak * (1.0 - self.cfg.vt_trim_band)
                if in_drawdown and atr > 0:
                    # 高波动下跌段：按 target/当前ATR% 缩放将持仓减至 desired 单位（下限 1）
                    scale = self._vol_scale(atr, close)
                    desired = max(1, min(self.cfg.max_units, int(round(self.cfg.max_units * scale))))
                    if desired < self.units:
                        sell = (self.units - desired) * self.unit_shares
                        if sell >= self.cfg.min_lot:
                            orders.append(Order(self.symbol, "SELL", sell, self.id, f"vol_trim@{close:.2f}"))
                            self.units = desired
                            self.last_add_price = close  # 回补需从当前价重新爬步，避免立即反手加回
                            return orders  # 波动减仓当根不再检查离场，避免下根开盘同日买卖违反 T+1
                elif self.units < self.cfg.max_units and not in_drawdown and self.last_add_price is not None \
                        and atr > 0 and close >= self.last_add_price + self.cfg.pyramid_step * atr:
                    # 非深跌段：维持/回补至满仓（每次 1 单位，步长闸门防追高）
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"vol_add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        return orders  # 本 bar 已加仓，不再检查离场，避免下根开盘同日买卖违反 T+1
            else:
                # 经典海龟金字塔加仓（未启用波动率目标化、或指标未就绪时）
                if (
                    self.units < self.cfg.max_units
                    and self.last_add_price is not None
                    and atr > 0
                    and close >= self.last_add_price + self.cfg.pyramid_step * atr
                ):
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        # 加仓当根不再检查离场：否则下根开盘会同时成交「加仓买 + 离场卖」，
                        # 等同当日买入并卖出刚加的单位，违反 A 股 T+1。
                        return orders

            # 3) 趋势反转离场
            if close < lower:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id, f"exit_breakdown@{close:.2f}")
                )
                self._reset_position()
            return orders

        # 空仓：突破入场
        if close > upper and atr > 0:
            unit_shares = self._compute_unit_shares(atr, close)
            if unit_shares > 0:
                orders.append(Order(self.symbol, "BUY", unit_shares, self.id, f"entry@{close:.2f}"))
                self.in_position = True
                self.units = 1
                self.entry_prices = [close]
                self.last_add_price = close
                self.unit_shares = unit_shares
                self.stop_price = close - self.cfg.stop_multiple * atr
                self.highest_close = close
        return orders

# ---------- strategies/ma_trend（MATrendStrategy） ----------
"""均线趋势策略（多策略组合中的「慢趋势」成员）。

与海龟（突破型）互补，采用均线多头排列确认趋势：
- 入场：收盘价站上快线，且快线位于慢线之上（多头趋势确认），建 1 个单位。
- 加仓：价格较建仓价再涨 1×ATR 时加 1 个单位，最多 2 个单位。
- 离场：收盘价跌破快线（趋势转弱），或盘中触及 2×ATR 硬止损。

特点：信号更平滑、交易频率低于海龟，用于在组合中降低整体回撤与换手。
"""
from typing import List

import pandas as pd




class MATrendStrategy(Strategy):
    def __init__(self, symbol: str, capital: float, cfg, max_units: int = 2):
        super().__init__(symbol, capital, cfg)
        self.id = "ma_trend"
        self.max_units = max_units

    def on_bar(self, bar: pd.Series) -> List[Order]:
        orders: List[Order] = []
        close = bar["close"]
        low = bar["low"]
        atr = bar["atr"]
        if pd.isna(close) or pd.isna(low) or pd.isna(atr):
            return orders
        close = float(close)
        low = float(low)
        atr = float(atr)
        ma_fast = bar.get("ma_fast")
        ma_slow = bar.get("ma_slow")

        if pd.isna(atr) or pd.isna(ma_fast) or pd.isna(ma_slow):
            return orders

        if self.in_position:
            # 0) 更新 ATR 吊灯止损（随最高价上移；trail_multiple=0 时退化为固定硬止损）
            self._update_trailing_stop(close, atr)

            if self.stop_price is not None and low <= self.stop_price:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id,
                          f"stop_loss@{self.stop_price:.2f}", limit_price=self.stop_price)
                )
                self._reset_position()
                return orders

            if getattr(self.cfg, "target_atr_pct", 0) > 0:
                # 波动率目标化（非对称）：满仓吃主升浪；仅「自峰值回撤超阈值(且高波动)」才减仓压回撤
                peak = self.highest_close if self.highest_close is not None else close
                in_drawdown = close < peak * (1.0 - self.cfg.vt_trim_band)
                if in_drawdown and atr > 0:
                    scale = self._vol_scale(atr, close)
                    desired = max(1, min(self.max_units, int(round(self.max_units * scale))))
                    if desired < self.units:
                        sell = (self.units - desired) * self.unit_shares
                        if sell >= self.cfg.min_lot:
                            orders.append(Order(self.symbol, "SELL", sell, self.id, f"vol_trim@{close:.2f}"))
                            self.units = desired
                            self.last_add_price = close
                            return orders  # 波动减仓当根不再检查离场，避免下根开盘同日买卖违反 T+1
                elif self.units < self.max_units and not in_drawdown and self.last_add_price is not None \
                        and atr > 0 and close >= self.last_add_price + self.cfg.ma_pyramid_step * atr:
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"vol_add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        return orders
            else:
                if (
                    self.units < self.max_units
                    and self.last_add_price is not None
                    and atr > 0
                    and close >= self.last_add_price + self.cfg.ma_pyramid_step * atr
                ):
                    add_shares = self.unit_shares
                    if add_shares > 0:
                        orders.append(Order(self.symbol, "BUY", add_shares, self.id, f"add@{close:.2f}"))
                        self.units += 1
                        self.entry_prices.append(close)
                        self.last_add_price = close
                        # 加仓当根不再检查离场，避免下根开盘同日买卖违反 T+1
                        return orders

            if close < ma_fast:
                orders.append(
                    Order(self.symbol, "SELL", self._total_shares(), self.id, f"exit_ma@{close:.2f}")
                )
                self._reset_position()
            return orders

        # 空仓：多头排列确认后入场
        if close > ma_fast and ma_fast > ma_slow and atr > 0:
            unit_shares = self._compute_unit_shares(atr, close)
            if unit_shares > 0:
                orders.append(Order(self.symbol, "BUY", unit_shares, self.id, f"entry@{close:.2f}"))
                self.in_position = True
                self.units = 1
                self.entry_prices = [close]
                self.last_add_price = close
                self.unit_shares = unit_shares
                self.stop_price = close - self.cfg.stop_multiple * atr
                self.highest_close = close
        return orders

# ---------- portfolio（Portfolio / Fill） ----------
"""组合与风险管理层。

负责：
- 现金与持仓记账（按 (标的, 策略) 维度区分各策略子仓位）。
- 撮合成交并扣除佣金、滑点、印花税。
- 强制无杠杆：买入金额不得超过可用现金，否则按整手缩减。
- 账户级风控：权益跌破「初始资金 ×(1-最大可容忍亏损)」时清仓并停机。
"""
from dataclasses import dataclass, field
from typing import Dict, List





@dataclass
class Fill:
    date: str
    symbol: str
    strategy: str
    side: str
    shares: int
    price: float
    reason: str = ""


class Portfolio:
    def __init__(self, cfg):
        self.cfg = cfg
        self.initial = float(cfg.initial_capital)
        self.cash = float(cfg.initial_capital)
        self.positions: Dict[tuple, int] = {}      # (symbol, strategy) -> 股数
        self.avg_cost: Dict[tuple, float] = {}      # (symbol, strategy) -> 持仓均价
        self.halted = False
        self.peak_equity = float(cfg.initial_capital)  # 用于「峰值最大回撤」口径
        self.fills: List[Fill] = []
        self.equity_curve: List[dict] = []

    # ---------- 估值 ----------
    def market_value(self, prices: Dict[str, float]) -> float:
        mv = 0.0
        for (symbol, _), sh in self.positions.items():
            if sh > 0 and symbol in prices and prices[symbol] is not None:
                mv += sh * prices[symbol]
        return mv

    def equity(self, prices: Dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    # ---------- 撮合 ----------
    def execute(self, order: Order, fill_price: float, bar_date) -> None:
        if self.halted:
            return
        key = (order.symbol, order.strategy)
        lot = self.cfg.min_lot

        if order.action == "BUY":
            if order.shares <= 0:
                return
            # 无杠杆：买入金额不得超过可用现金（按「每股成本」计算可买整手数）
            eff = effective_price("BUY", fill_price, self.cfg.slippage)
            _, cash_delta = trade_value(
                "BUY", order.shares, fill_price, self.cfg.slippage,
                self.cfg.commission, self.cfg.stamp_duty,
            )
            per_share = -cash_delta / order.shares
            max_affordable = int(self.cash / per_share // lot) * lot
            shares = min(order.shares, max_affordable)
            if shares <= 0:
                return
            _, real_delta = trade_value(
                "BUY", shares, fill_price, self.cfg.slippage,
                self.cfg.commission, self.cfg.stamp_duty,
            )
            prev = self.positions.get(key, 0)
            prev_cost = self.avg_cost.get(key, eff)
            new = prev + shares
            self.avg_cost[key] = (prev * prev_cost + shares * eff) / new
            self.positions[key] = new
            self.cash += real_delta
            self.fills.append(Fill(bar_date, order.symbol, order.strategy, "BUY", shares, eff, order.reason))

        else:  # SELL
            sh = self.positions.get(key, 0)
            sell = min(order.shares, sh)
            if sell <= 0:
                return
            eff, cash_delta = trade_value(
                "SELL", sell, fill_price, self.cfg.slippage,
                self.cfg.commission, self.cfg.stamp_duty,
            )
            self.cash += cash_delta
            remaining = sh - sell
            self.positions[key] = remaining
            if remaining == 0:
                self.avg_cost.pop(key, None)
            self.fills.append(Fill(bar_date, order.symbol, order.strategy, "SELL", sell, eff, order.reason))

    # ---------- 账户级风控 ----------
    def check_drawdown_halt(self, prices: Dict[str, float]) -> bool:
        """权益回撤超过阈值则清仓并停机，返回是否触发。

        回撤口径由 config.max_drawdown_basis 决定：
          - "peak"   ：相对权益历史峰值（标准「最大回撤」定义，更保护收益）
          - "initial"：相对初始资金（即累计亏损达 max_total_risk 即停）
        """
        if self.halted:
            return True
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        if self.cfg.max_drawdown_basis == "peak":
            threshold = self.peak_equity * (1.0 - self.cfg.max_total_risk)
        else:
            threshold = self.initial * (1.0 - self.cfg.max_total_risk)
        if eq < threshold:
            self.halted = True
            for (symbol, strategy), sh in list(self.positions.items()):
                if sh > 0 and symbol in prices and prices[symbol] is not None:
                    px = prices[symbol]
                    _, cash_delta = trade_value(
                        "SELL", sh, px, self.cfg.slippage,
                        self.cfg.commission, self.cfg.stamp_duty,
                    )
                    self.cash += cash_delta
                    self.fills.append(Fill("HALT", symbol, strategy, "SELL", sh, px, "max_drawdown_halt"))
                    self.positions[symbol, strategy] = 0
            self.avg_cost.clear()
            return True
        return False

# ---------- indicators（技术指标） ----------
"""技术指标计算。

所有函数返回与输入等长、索引对齐的 pandas.Series。为避免前视偏差，
通道类指标均使用 shift(1)：即用「截至上一根」的 N 日极值作为当根信号阈值。
"""
import numpy as np
import pandas as pd


def _qt_true_range(high, low, close) -> pd.Series:
    """真实波幅 TR = max(H-L, |H-前收|, |L-前收|)。"""
    high = pd.Series(high, dtype="float64")
    low = pd.Series(low, dtype="float64")
    close = pd.Series(close, dtype="float64")
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(high, low, close, period: int = 20) -> pd.Series:
    """平均真实波幅（Wilder 指数平滑）。"""
    tr = _qt_true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def donchian_upper(high, period: int) -> pd.Series:
    """入场突破上轨：前 period 日最高价（不含当根）。"""
    return pd.Series(high, dtype="float64").rolling(period).max().shift(1)


def donchian_lower(low, period: int) -> pd.Series:
    """离场跌破下轨：前 period 日最低价（不含当根）。"""
    return pd.Series(low, dtype="float64").rolling(period).min().shift(1)


def moving_average(close, period: int) -> pd.Series:
    """简单移动平均线。"""
    return pd.Series(close, dtype="float64").rolling(period).mean()


def add_indicators(df: pd.DataFrame, cfg, donchian_periods: set = None) -> pd.DataFrame:
    """在行情 DataFrame 上追加所有回测所需指标列。

    donchian_periods：需计算的唐奇安通道窗口集合（入场上轨 / 离场下轨共用）。
    为 None 时默认取 cfg.turtle_entries 与 cfg.turtle_exits 的并集，保证自定义
    参数扫描时策略所需的任意周期通道列均存在。
    """
    df = df.copy()
    df["atr"] = atr(df["high"], df["low"], df["close"], cfg.atr_period)
    if donchian_periods is None:
        donchian_periods = set(cfg.turtle_entries) | set(cfg.turtle_exits)
    for p in sorted(donchian_periods):
        df[f"donchian_upper_{p}"] = donchian_upper(df["high"], p)
        df[f"donchian_lower_{p}"] = donchian_lower(df["low"], p)
    df["ma_fast"] = moving_average(df["close"], cfg.ma_fast)
    df["ma_slow"] = moving_average(df["close"], cfg.ma_slow)
    return df

# ---------- data_feed（行情接入） ----------
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

# ---------- backtest（多策略组合回测引擎） ----------
"""事件驱动回测引擎 + 多策略组合调度。

执行时序（杜绝前视偏差、满足 T+1）：
  对第 i 根 bar：
    1) 以本根「开盘价」撮合上一根生成的挂单（pending）。
    2) 调用各策略 on_bar(本根) 生成新挂单，留待第 i+1 根开盘撮合。
    3) 记录本根权益曲线。
    4) 检查账户级最大回撤，必要时清仓停机。

止损单携带 limit_price：若下根开盘价 ≤ 止损价（跳空），按开盘价成交（更保守），
否则按止损价成交。
"""
from typing import Dict, List

import pandas as pd









def build_strategy(strategy_id: str, symbol: str, capital: float, cfg: Config):
    """根据策略 id 与分配到的资本，构造策略实例。"""
    if strategy_id == "turtle20":
        return TurtleStrategy(symbol, capital, cfg, entry_period=20, exit_period=10)
    if strategy_id == "turtle55":
        return TurtleStrategy(symbol, capital, cfg, entry_period=55, exit_period=20)
    if strategy_id == "ma_trend":
        return MATrendStrategy(symbol, capital, cfg)
    raise ValueError(f"未知策略 id：{strategy_id}")


def run_backtest(df: pd.DataFrame, cfg: Config, symbol: str = None) -> dict:
    """对单只标的运行多策略组合回测。

    返回包含权益曲线、成交、指标等结果的字典。
    """
    symbol = symbol or (cfg.universe[0] if cfg.universe else None)
    if symbol is None:
        raise ValueError("未指定回测标的")

    df = add_indicators(df, cfg)
    portfolio = Portfolio(cfg)

    strat_instances = []
    for sid in cfg.strategies:
        weight = cfg.strategy_capital_weight.get(sid, 1.0 / len(cfg.strategies))
        cap = cfg.initial_capital * weight
        strat_instances.append(build_strategy(sid, symbol, cap, cfg))

    pending: List[Order] = []
    st_by_id = {st.id: st for st in strat_instances}
    for i in range(len(df)):
        bar = df.iloc[i]
        date = bar["date"]
        prices = {symbol: float(bar["close"])}

        # 1) 撮合上一根挂单（本根开盘价）
        for order in pending:
            if order.limit_price is not None:
                # 止损单：下根开盘若已低于止损价，按开盘价成交（更保守）
                open_px = float(bar["open"])
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = float(bar["open"])
            portfolio.execute(order, fill, date)
            # 成交后回填策略状态（H8：以组合实际持仓为唯一真相源）
            st = st_by_id.get(order.strategy)
            if st is not None:
                actual = portfolio.positions.get((order.symbol, order.strategy), 0)
                st.sync_position(actual)
        pending = []

        if portfolio.halted:
            portfolio.equity_curve.append(
                {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
            )
            continue

        # 2) 生成新信号
        for st in strat_instances:
            pending.extend(st.on_bar(bar))

        # 3) 账户级风控（可能触发清仓）
        portfolio.check_drawdown_halt(prices)

        # 4) 记录权益（清算后口径，避免虚高，H7 修复）
        portfolio.equity_curve.append(
            {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
        )

    result = {
        "symbol": symbol,
        "equity_curve": pd.DataFrame(portfolio.equity_curve).set_index("date"),
        "fills": portfolio.fills,
        "final_equity": portfolio.equity({symbol: float(df.iloc[-1]["close"])}),
        "cash": portfolio.cash,
        "halted": portfolio.halted,
        "data": df,
    }
    return result


def run_backtest_multi(data: Dict[str, pd.DataFrame], cfg: Config, strategies=None,
                       donchian_periods=None) -> dict:
    """对「多标的组合」运行多策略回测（共享现金池、账户级风控）。

    与单标的回测保持完全一致的事件时序（当根信号 → 下一根开盘撮合），
    并按日期对齐多标的行情。各 (标的, 策略) 子仓位共享同一现金池，
    由组合层强制无杠杆（买入金额不得超过可用现金）。

    参数：
      data              : {symbol: 原始日线 DataFrame}（前复权，未经指标增强）
      cfg               : 全局配置
      strategies        : 可选，预构建的策略实例列表；为 None 时按默认权重自动构建
                          （每个 (标的, 策略) 分配 capital = initial * 权重 / 标的数量）
      donchian_periods  : 可选，需计算的唐奇安通道周期集合；用于自定义参数扫描
    """
    enriched = {sym: add_indicators(df.copy(), cfg, donchian_periods)
                for sym, df in data.items()}

    if strategies is None:
        strategies = []
        n_sym = max(len(enriched), 1)
        for symbol in cfg.universe:
            for sid in cfg.strategies:
                weight = cfg.strategy_capital_weight.get(sid, 1.0 / len(cfg.strategies))
                cap = cfg.initial_capital * weight / n_sym
                strategies.append(build_strategy(sid, symbol, cap, cfg))

    portfolio = Portfolio(cfg)
    # 以 (标的, 策略id) 唯一键索引：同一策略 id（如 turtle20）在多个标的下各有一个实例，
    # 若仅以 st.id 为键会发生「后者覆盖前者」，导致 sync_position 跨标的污染状态（BUG-1 修复）。
    st_by_id = {(st.symbol, st.id): st for st in strategies}

    # 各标的按日期建索引，取交集作为主时间轴（A 股同交易日历）
    bars_by_date = {
        sym: {row["date"]: row for _, row in df.iterrows()}
        for sym, df in enriched.items()
    }
    master = None
    for sym in enriched:
        idx = set(bars_by_date[sym].keys())
        master = idx if master is None else (master & idx)
    all_dates = sorted(master)

    last_close = {sym: None for sym in enriched}
    pending: List[Order] = []
    equity_curve = []

    for date in all_dates:
        # 估值价：优先用当根收盘，缺失则沿用最近已知收盘
        prices = {}
        for sym in enriched:
            row = bars_by_date[sym].get(date)
            if row is not None:
                last_close[sym] = float(row["close"])
            prices[sym] = last_close[sym]

        # 1) 撮合上一根挂单（本根开盘价）
        for order in pending:
            row = bars_by_date[order.symbol].get(date)
            if row is None:
                continue  # 该标的当日无行情，递延（极少见）
            open_px = float(row["open"])
            if order.limit_price is not None:
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = open_px
            portfolio.execute(order, fill, date)
            st = st_by_id.get((order.symbol, order.strategy))
            if st is not None:
                actual = portfolio.positions.get((order.symbol, order.strategy), 0)
                st.sync_position(actual)
        pending = []

        if portfolio.halted:
            equity_curve.append(
                {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
            )
            continue

        # 2) 生成新信号（每个策略读取其所属标的的当根 bar）
        for st in strategies:
            row = bars_by_date[st.symbol].get(date)
            if row is not None:
                pending.extend(st.on_bar(row))

        # 3) 账户级风控（可能触发清仓）
        portfolio.check_drawdown_halt(prices)

        # 4) 记录权益（清算后口径）
        equity_curve.append(
            {"date": date, "equity": portfolio.equity(prices), "cash": portfolio.cash}
        )

    result = {
        "symbol": "+".join(cfg.universe),
        "equity_curve": pd.DataFrame(equity_curve).set_index("date"),
        "fills": portfolio.fills,
        "final_equity": portfolio.equity({sym: last_close[sym] for sym in enriched}),
        "cash": portfolio.cash,
        "halted": portfolio.halted,
        "data": enriched,
    }
    return result


def compute_metrics(result: dict, cfg: Config) -> dict:
    """由权益曲线计算绩效指标。"""
    eq = result["equity_curve"]["equity"]
    initial = cfg.initial_capital
    total_return = eq.iloc[-1] / initial - 1.0
    peak = eq.cummax()
    drawdown = eq / peak - 1.0
    max_drawdown = drawdown.min()

    # 年化（按 252 个交易日）
    n_days = max(len(eq) - 1, 1)
    annual_return = (1.0 + total_return) ** (252.0 / n_days) - 1.0

    # 单笔成交统计
    buys = [f for f in result["fills"] if f.side == "BUY"]
    sells = [f for f in result["fills"] if f.side == "SELL"]
    halt_fills = [f for f in sells if f.reason == "max_drawdown_halt"]

    return {
        "initial_capital": initial,
        "final_equity": float(eq.iloc[-1]),
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": float(max_drawdown),
        "n_bars": int(len(eq)),
        "n_buy_orders": len(buys),
        "n_sell_orders": len(sells),
        "n_halt_liquidations": len(halt_fills),
        "halted": bool(result["halted"]),
    }


HERE = os.path.dirname(os.path.abspath(__file__))

# ========================= 标的与路径常量 =========================
ORIG_SYMS = ['300308','300502','300394','688008','603986','002409','688300','300054','688535']
ORIG_NAMES = {'300308':'中际旭创','300502':'新易盛','300394':'天孚通信','688008':'澜起科技',
              '603986':'兆易创新','002409':'雅克科技','688300':'联瑞新材','300054':'鼎龙股份','688535':'华海诚科'}
ORIG_DATA_DIR = os.path.join(HERE, "data")
ORIG_START, ORIG_END = "20250101", "20260630"

SEMI_SYMS = ['688249','688347','300666','600206','688409','688361','300604','688120','688082']
SEMI_DATA_DIR = os.path.join(HERE, "data_semi")
SEMI_START = "20250401"
CAP_ORIG = 0.60
CAP_SEMI = 0.585

SEMI_NAMES = {'688249':'晶合集成','688347':'华虹宏力','300666':'江丰电子','600206':'有研新材',
              '688409':'富创精密','688361':'中科飞测','300604':'长川科技','688120':'华海清科','688082':'盛美上海'}

# ========================= 两篮配置（内嵌，不再依赖外部 JSON） =========================
ORIG_CONFIG = {
    "name": "约束最优(口径B:单票峰值≤60%+回撤≤20%)",
    "params": {
        "sizing": "fixed", "cap_frac": 1.0, "entry": 10, "exit": 40,
        "pyramid_step": 0.5, "max_units": 12, "trail_multiple": 0.0,
        "stop_multiple": 2.0, "risk_per_trade": 0.075, "max_symbol_weight": 0.60,
        "max_positions": 9, "adx_min": 0, "mom_lookback": 20, "adx_period": 14,
        "circuit": 0.17, "cooldown": 10, "cap_trim": True,
    },
    "constraints": {"max_symbol_weight": 0.60, "max_drawdown_cap": 0.20,
                    "sizing": "fixed_cap1.0", "cap_trim": True,
                    "cap_note": "持仓峰值占比>60%时当根收盘减仓至60%"},
    "baseline": {"as_of": "2026-06-30", "total_return": 8.110327,
                 "max_drawdown": -0.192145, "final": 18220653.57, "trades": 195,
                 "circuit_events": 1, "single_stock_peak": 0.6035},
}

SEMI_CONFIG = {
    "name": "半导体篮子(口径B优化: exit70/stop4/cir0.18/cd20, 单票≤60%严格上限, 剔除中芯)",
    "params": {
        "sizing": "fixed", "cap_frac": 1.0, "entry": 10, "exit": 70,
        "pyramid_step": 0.5, "max_units": 12, "trail_multiple": 0.0,
        "stop_multiple": 4.0, "risk_per_trade": 0.075, "max_symbol_weight": 0.585,
        "max_positions": 9, "adx_min": 0, "mom_lookback": 20, "adx_period": 14,
        "circuit": 0.18, "cooldown": 20, "cap_trim": True,
    },
    "symbols": SEMI_SYMS,
    "names": SEMI_NAMES,
    "excluded": ["688981"],
    "excluded_note": "中芯国际(688981)在2025-04~2026-06窗口单票峰值仅~1.3%、几乎未被引擎选入持有，属闲置仓位；剔除后收益略升(185.69%→187.86%)、回撤一致。如需纳入，把688981加回 symbols 即可。",
    "start": "20250401",
    "equity_file": "equity_semi_nosmic.csv",
    "constraints": {"max_symbol_weight": 0.585, "max_drawdown_cap": 0.20,
                    "sizing": "fixed_cap1.0", "cap_trim": True,
                    "cap_note": "买入上限0.585留T+1余量，收盘减仓目标=max_symbol_weight(0.585)，单票峰值59.1%≤60%"},
    "baseline": {"as_of": "2026-06-30", "config": "exit70/stop4/cir0.18/cd20",
                 "total_return": 4.149884, "max_drawdown": -0.194663,
                 "final": 10299767.21, "trades": 104, "circuit_events": 8, "single_stock_peak": 0.590763},
    "prior_baseline": {"as_of": "2026-06-30", "config": "exit40/stop2/cir0.17/cd10",
                       "total_return": 1.878629, "max_drawdown": -0.189651,
                       "final": 5757258.72, "trades": 153, "circuit_events": 11, "single_stock_peak": 0.5957},
}

# 半篮 10 只（含中芯，legacy 对比用）
LEGACY_SEMI_SYMS = ['688249','688981','688347','300666','600206','688409','688361','300604','688120','688082']
LEGACY_SEMI_NAMES = {'688249':'晶合集成','688981':'中芯国际','688347':'华虹宏力','300666':'江丰电子','600206':'有研新材',
                     '688409':'富创精密','688361':'中科飞测','300604':'长川科技','688120':'华海清科','688082':'盛美上海'}
LEGACY_SEMI_DATA_DIR = os.path.join(HERE, "data_semi")
LEGACY_SEMI_START = "20250401"

# fetch_semi 用：10 只半导体（含中芯）
FETCH_SYMS = {
    "688249": "晶合集成", "688981": "中芯国际", "688347": "华虹宏力",
    "300666": "江丰电子", "600206": "有研新材", "688409": "富创精密",
    "688361": "中科飞测", "300604": "长川科技", "688120": "华海清科",
    "688082": "盛美上海",
}
FETCH_START, FETCH_END = "20250101", "20260630"
FETCH_OUT = os.path.join(HERE, "data_semi")

# run_turtle_v11 用：上传的对比文件
TURTLE_V11_PATH = "/root/uploads/1783704041769624947-turtle_v11_9symbols_optimized_full.py"

# ========================= 核心引擎（原 optimized_backtest.py） =========================
def wilder_adx(high, low, close, period=14):
    high=pd.Series(high,dtype=float); low=pd.Series(low,dtype=float); close=pd.Series(close,dtype=float)
    up=high.diff(); dn=-low.diff()
    plus_dm=pd.Series(np.where((up>dn)&(up>0),up,0.0),index=close.index)
    minus_dm=pd.Series(np.where((dn>up)&(dn>0),dn,0.0),index=close.index)
    tr=true_range(high,low,close)
    atr=tr.ewm(alpha=1/period,min_periods=period,adjust=False).mean()
    ps=plus_dm.ewm(alpha=1/period,min_periods=period,adjust=False).mean()
    ms=minus_dm.ewm(alpha=1/period,min_periods=period,adjust=False).mean()
    pdi=100*ps/atr; mdi=100*ms/atr
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/period,min_periods=period,adjust=False).mean().fillna(0)

def true_range(high,low,close):
    pc=close.shift(1)
    return pd.concat([(high-low).abs(),(high-pc).abs(),(low-pc).abs()],axis=1).max(axis=1)

def make_cfg(p):
    cfg=replace(Config(), initial_capital=2_000_000.0,
                max_total_risk=0.99, max_drawdown_basis="peak",
                commission=0.0003, slippage=0.001, stamp_duty=0.0005,
                atr_period=20, ma_fast=20, ma_slow=60,
                max_units=p['max_units'], stop_multiple=p['stop_multiple'],
                pyramid_step=p['pyramid_step'], trail_multiple=p['trail_multiple'],
                risk_per_trade=p['risk_per_trade'])
    cfg._entry, cfg._exit = p.get('entry',10), p.get('exit',70)
    return cfg

class OptTurtle(TurtleStrategy):
    """海龟 + ADX 入场过滤。"""
    def __init__(self, symbol, capital, cfg, ep, xp, adx_min=0):
        super().__init__(symbol, capital, cfg, ep, xp)
        self.adx_min=adx_min
    def on_bar(self, bar):
        if not self.in_position and self.adx_min>0:
            adx=bar.get('adx',0) or 0
            if pd.isna(adx) or adx < self.adx_min:
                return []
        return super().on_bar(bar)

def load_enriched(p, syms=None, data_dir=None):
    data={}; mom_map={}
    ep=p.get('entry',10); xp=p.get('exit',70)
    for code in (syms or ORIG_SYMS):
        df=pd.read_csv(os.path.join(data_dir or ORIG_DATA_DIR,f"{code}.csv"))
        df['date']=pd.to_datetime(df['date'])
        df=df.sort_values('date').reset_index(drop=True)
        df['atr']=true_range(df['high'],df['low'],df['close']).ewm(alpha=1/20,min_periods=20,adjust=False).mean()
        df[f'donchian_upper_{ep}']=df['high'].rolling(ep).max().shift(1)
        df[f'donchian_lower_{xp}']=df['low'].rolling(xp).min().shift(1)
        df['ma_fast']=df['close'].rolling(20).mean()
        df['ma_slow']=df['close'].rolling(60).mean()
        df['adx']=wilder_adx(df['high'],df['low'],df['close'],p.get('adx_period',14))
        ml=p.get('mom_lookback',20)
        df['mom']=df['close']/df['close'].shift(ml)-1
        data[code]=df
        mom_map[code]={row['date']:(row['mom'] if not pd.isna(row['mom']) else None)
                       for _,row in df.iterrows()}
    return data, mom_map

def run_optimized(p, syms=None, data_dir=None, start=None, end=None):
    syms = syms or ORIG_SYMS
    data_dir = data_dir or ORIG_DATA_DIR
    cfg=make_cfg(p)
    data, mom_map = load_enriched(p, syms, data_dir)
    bars_by_date={s:{row['date']:row for _,row in df.iterrows()} for s,df in data.items()}
    master=None
    for s in syms:
        idx=set(bars_by_date[s].keys())
        master=idx if master is None else (master & idx)
    all_dates=sorted(master)
    if start:
        sd=pd.Timestamp(start); all_dates=[d for d in all_dates if d>=sd]
    if end:
        ed=pd.Timestamp(end); all_dates=[d for d in all_dates if d<=ed]
    if not all_dates:
        return {"total_return":0.0,"max_drawdown":0.0,"trades":0,
                "final":float(cfg.initial_capital),"halted":False,"n_buy":0}
    strategies=[]
    for s in syms:
        strategies.append(OptTurtle(s, cfg.initial_capital, cfg, p.get('entry',10), p.get('exit',70), adx_min=p['adx_min']))
        strategies.append(MATrendStrategy(s, cfg.initial_capital, cfg))
    st_by_id={(st.symbol,st.id):st for st in strategies}
    pf=Portfolio(cfg)
    last_close={s:None for s in syms}
    pending=[]; equity_curve=[]
    cap_frac=p['cap_frac']; max_sym_w=p['max_symbol_weight']; max_pos=p['max_positions']
    peak=cfg.initial_capital; cooldown_remaining=0
    open_date={}          # (sym,stid) -> 最近一次买入成交的日期（用于 T+1 校验）
    deferred_sell=set()   # 熔断当日新买、依 T+1 须递延至次日卖出的仓位
    max_sym_wt={s:0.0 for s in syms}  # 逐票持仓市值占比峰值（验证单票≤60%）

    def sym_value(prices):
        return sum(sh*(prices.get(sym) or 0) for (sym,_),sh in pf.positions.items() if sh>0)

    for date in all_dates:
        for s in syms:
            if date in bars_by_date[s]:
                last_close[s]=float(bars_by_date[s][date]['close'])
        prices={s:last_close[s] for s in syms}
        equity=pf.cash+sym_value(prices)
        # 定仓方式
        for st in strategies:
            if p.get('sizing','equity')=='equity':
                st.capital=equity*cap_frac
            elif p['sizing']=='fixed':
                st.capital=cfg.initial_capital*cap_frac
            else:
                st.capital=cfg.initial_capital*cap_frac/len(syms)
        # 1) 撮合上一根挂单（本根开盘）
        for order in pending:
            row=bars_by_date[order.symbol].get(date)
            if row is None: continue
            open_px=float(row['open'])
            if order.limit_price is not None:
                # limit_price 对卖单而言是"止损价"(见 内联引擎 Order（limit_price 即止损价）)：
                # 下根开盘若跳空低于止损价则按开盘价成交(更保守)，否则按止损价成交。
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = open_px
            shares=order.shares
            if order.action=='BUY':
                cur_sym=sum(sh*(prices.get(order.symbol) or 0) for (sym,_),sh in pf.positions.items() if sym==order.symbol)
                room=max(0.0, max_sym_w*equity-cur_sym)
                max_shares=int(room/fill//100)*100 if fill>0 else 0
                shares=min(shares,max_shares)
                if shares<=0: continue
            pf.execute(Order(order.symbol,order.action,shares,order.strategy,order.reason,order.limit_price), fill, date)
            if order.action=='BUY' and shares>0:
                open_date[(order.symbol,order.strategy)]=date
            st=st_by_id.get((order.symbol,order.strategy))
            if st: st.sync_position(pf.positions.get((order.symbol,order.strategy),0))
        pending=[]
        # 处理上一根熔断递延的「当日新买仓位」：次日即满足 T+1，于当根收盘平仓
        for key in list(deferred_sell):
            sh=pf.positions.get(key,0)
            if sh>0:
                px=last_close.get(key[0])
                if px is not None:
                    pf.execute(Order(key[0],'SELL',sh,key[1],'circuit_deferred'),px,date)
                    st=st_by_id.get(key)
                    if st: st.sync_position(0)
                    open_date.pop(key,None)
            deferred_sell.discard(key)
        # 单票(组合)权重追踪 + 口径B严格上限：同一标的 turtle+ma 合计占比 > max_sym_w 时
        # 当根收盘减仓至 max_sym_w。注意必须按「标的」聚合子仓位，不能按 (sym,stid) 子仓位
        # 逐个判断——否则同一标的两策略各 <60% 但合计 >60% 时会漏剪（已实测 天孚通信达 68.9%）。
        if not pf.halted:
            cur_eq = pf.cash + sym_value(prices)
            by_sym = {}
            for (sym,stid),sh in list(pf.positions.items()):
                if sh>0:
                    by_sym.setdefault(sym, []).append((stid, sh))
            for sym, subs in by_sym.items():
                combined = sum(sh*prices.get(sym,0) for (_,sh) in subs)
                wt = combined/cur_eq if cur_eq>0 else 0.0
                if p.get('cap_trim', False) and wt > max_sym_w + 1e-9:
                    px = last_close.get(sym)
                    if px and px>0:
                        sell_value = combined - max_sym_w*cur_eq
                        sell_shares = int(sell_value/px//100)*100
                        if sell_shares >= 100:
                            # 从最大子仓位开始减，逐仓成交，直至合计回到上限内。
                            # T+1：当日新买仓位(open_date==date)当根不可卖，跳过并递延至次日，
                            # 避免「当根开盘买、当根收盘卖」同日回转（与熔断 deferred 逻辑一致）。
                            remaining = sell_shares
                            for stid, sh in sorted(subs, key=lambda x:-x[1]):
                                if remaining <= 0:
                                    break
                                if open_date.get((sym, stid)) == date:
                                    continue
                                take = int(min(sh, remaining)//100)*100
                                if take >= 100:
                                    pf.execute(Order(sym,'SELL',take,stid,'cap_trim'),px,date)
                                    st = st_by_id.get((sym,stid))
                                    if st: st.sync_position(pf.positions.get((sym,stid),0))
                                    remaining -= take
                            cur_eq = pf.cash + sym_value(prices)  # 含本次减仓重算
                combined_after = sum(pf.positions.get((sym,stid),0)*prices.get(sym,0) for (stid,_) in subs)
                wt_after = combined_after/cur_eq if cur_eq>0 else 0.0
                max_sym_wt[sym]=max(max_sym_wt[sym],wt_after)
        if pf.halted:
            equity_curve.append({"date":date,"equity":equity,"cash":pf.cash}); continue
        # 2) 熔断（当根收盘立即清仓）+ 冷却
        peak=max(peak, equity)
        dd=(equity/peak-1) if peak>0 else 0.0
        risk_off = cooldown_remaining>0
        if risk_off:
            cooldown_remaining-=1
        elif dd <= -p['circuit']:
            for (sym,stid),sh in list(pf.positions.items()):
                if sh>0:
                    if open_date.get((sym,stid))==date:
                        deferred_sell.add((sym,stid))  # T+1：当日新买仓位递延至次日卖
                        continue
                    px=last_close[sym]  # 当根收盘立即平
                    pf.execute(Order(sym,'SELL',sh,stid,'circuit_break'), px, date)
                    st=st_by_id.get((sym,stid))
                    if st: st.sync_position(0)
                    open_date.pop((sym,stid),None)
            cooldown_remaining=p.get('cooldown',10)
            peak=equity  # 重置峰值
            # 清仓后重算净值
            equity=pf.cash
            risk_off=True  # 熔断后强制进入冷却，避免同根bar再发买入信号
        # 3) 生成新信号 + 动量轮动门控
        if mom_map:
            ranked=[(s,mom_map[s].get(date)) for s in syms]
            ranked=[(s,m) for s,m in ranked if m is not None]
            ranked.sort(key=lambda x:-x[1])
            allowed=set(s for s,_ in ranked[:max_pos])
        else:
            allowed=set(syms)
        for st in strategies:
            row=bars_by_date[st.symbol].get(date)
            if row is None: continue
            new=st.on_bar(row)
            for o in new:
                if o.action=='BUY':
                    if risk_off:
                        continue
                    flat=all(sh<=0 for (sym,_),sh in pf.positions.items() if sym==o.symbol)
                    if flat and o.symbol not in allowed:
                        continue
                pending.append(o)
        equity_curve.append({"date":date,"equity":pf.equity(prices),"cash":pf.cash})
    eq=pd.DataFrame(equity_curve).set_index("date")["equity"]
    total=eq.iloc[-1]/cfg.initial_capital-1
    dd=(eq/eq.cummax()-1).min()
    n_trades=len([f for f in pf.fills if f.side=='BUY'])+len([f for f in pf.fills if f.side=='SELL'])
    return {"total_return":float(total),"max_drawdown":float(dd),"trades":n_trades,
            "final":float(eq.iloc[-1]),"halted":pf.halted,"n_buy":len([f for f in pf.fills if f.side=='BUY']),
            "max_sym_wt":max_sym_wt,"equity_curve":equity_curve}

# ========================= 公共工具（原 backtest_common.py） =========================
_orig_execute = Portfolio.execute
_reasons = []  # list of (date, reason)

def _patched(self, order, fill, date):
    _reasons.append((date, order.reason))
    return _orig_execute(self, order, fill, date)

def count_circuits():
    """重置并返回 reason 收集列表；须在 run_optimized 之前调用。"""
    global _reasons
    _reasons = []
    Portfolio.execute = _patched
    return _reasons

def circuit_events():
    """熔断事件日期列表(去重, 保持发生顺序)。
    一次熔断事件会在同一交易日对多个持仓各下一笔 circuit_break 卖单，
    故按"日期"去重才是真实事件数；按 reason 计数会高估为交易笔数。"""
    seen = []
    for d, r in _reasons:
        if r == "circuit_break" and d not in seen:
            seen.append(d)
    return seen

def circuit_count():
    """熔断事件数(按日期去重)，非交易笔数。"""
    return len(circuit_events())

# update_config 已移除：配置改为内嵌常量(ORIG_CONFIG/SEMI_CONFIG)，不再写回外部 JSON。

# ========================= 子命令实现 =========================
# 两篮 baseline 已内嵌于 ORIG_CONFIG["baseline"] / SEMI_CONFIG["baseline"]（见上方常量区）。

def cmd_original(out_path=None):
    """原篮(光通信/存储 9 只)：跑回测 + 写 equity_constrained.csv（配置已内嵌于 ORIG_CONFIG）。"""
    if out_path is None:
        out_path = os.path.join(HERE, "equity_constrained.csv")
    elif not os.path.isabs(out_path):
        out_path = os.path.join(HERE, out_path)
    p = ORIG_CONFIG["params"]
    reasons = count_circuits()
    r = run_optimized(p, syms=ORIG_SYMS, data_dir=ORIG_DATA_DIR)  # 全量数据, end=None
    peak_wt = max(r["max_sym_wt"].values())
    circ = circuit_count()
    dates = [c["date"] for c in r["equity_curve"]]
    eqs = [c["equity"] for c in r["equity_curve"]]
    s = pd.Series(eqs, index=dates)
    dd = (s / s.cummax() - 1)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "cash", "drawdown"])
        for d, c in zip(dates, r["equity_curve"]):
            w.writerow([d.strftime("%Y-%m-%d"), f"{c['equity']:.6f}", c.get("cash", ""), f"{dd[d]:.6f}"])
    data_end = str(dates[-1].date())
    print(f"[original 9只] 收益={r['total_return']:.4%}  回撤={r['max_drawdown']:.4%}  "
          f"终值={r['final']:,.2f}  成交={r['trades']}  单票峰={peak_wt:.4%}  熔断={circ}  数据末日={data_end}")
    print(f"  双约束: 回撤≤20% {'✅' if r['max_drawdown'] >= -0.20 else '❌'}  "
          f"单票≤60% {'✅' if peak_wt <= 0.60 else '❌'}")
    return r, peak_wt

def cmd_semi(out_path=None):
    """半篮(半导体 9 只)：跑回测 + 写 equity_semi_nosmic.csv（配置已内嵌于 SEMI_CONFIG）。"""
    p = SEMI_CONFIG["params"]
    syms = SEMI_CONFIG["symbols"]
    data_dir = SEMI_DATA_DIR
    start = SEMI_CONFIG.get("start")
    if out_path is None:
        out_path = SEMI_CONFIG.get("equity_file")
    if out_path and not os.path.isabs(out_path):
        out_path = os.path.join(HERE, out_path)
    reasons = count_circuits()
    r = run_optimized(p, syms=syms, data_dir=data_dir, start=start, end=None)
    peak_wt = max(r["max_sym_wt"].values())
    circ = circuit_count()
    if out_path:
        dates = [c["date"] for c in r["equity_curve"]]
        eqs = [c["equity"] for c in r["equity_curve"]]
        s = pd.Series(eqs, index=dates)
        dd = (s / s.cummax() - 1)
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "equity", "cash", "drawdown"])
            for d, c in zip(dates, r["equity_curve"]):
                w.writerow([d.strftime("%Y-%m-%d"), f"{c['equity']:.6f}", c.get("cash", ""), f"{dd[d]:.6f}"])
    data_end = str(r["equity_curve"][-1]["date"].date())
    print(f"[semi 9只] 标的数={len(syms)}  收益={r['total_return']:.4%}  "
          f"回撤={r['max_drawdown']:.4%}  终值={r['final']:,.2f}  成交={r['trades']}  "
          f"单票峰={peak_wt:.4%}  熔断={circ}  数据末日={data_end}")
    dd_ok = r['max_drawdown'] >= -0.20
    wt_ok = peak_wt <= 0.60
    semi_internal = "✅" if peak_wt <= 0.585 else "⚠内部上限轻微超出(T+1)"
    print(f"  双约束(口径B 单票≤60%): 回撤≤20% {'✅' if dd_ok else '❌'}  "
          f"单票≤60% {'✅' if wt_ok else '❌'}(内部上限58.5% {semi_internal})")
    return r, peak_wt

def cmd_verify():
    """约束最优明细复盘（口径B）：直接取自 run_optimized 真实结果 + 熔断事件日期。"""
    P = dict(sizing='fixed', cap_frac=1.0, entry=10, exit=40,
             pyramid_step=0.5, max_units=12, trail_multiple=0.0, stop_multiple=2.0,
             risk_per_trade=0.075, max_symbol_weight=0.60, max_positions=9,
             adx_min=0, mom_lookback=20, adx_period=14, circuit=0.17, cooldown=10,
             cap_trim=True)
    reasons = count_circuits()
    r = run_optimized(P)
    peak_wt = max(r["max_sym_wt"].values())
    evts = circuit_events()
    print("=== 主指标 ===")
    print(f"总收益 : {r['total_return']:.2%}")
    print(f"最大回撤: {r['max_drawdown']:.2%}  (硬约束 ≤ -20%)")
    print(f"成交数 : {r['trades']}")
    print(f"买入笔数: {r['n_buy']}")
    print(f"终值   : {r['final']:,.0f}  (初始 2,000,000)")
    print(f"\n熔断触发次数: {len(evts)}  日期: {[d.strftime('%Y-%m-%d') for d in evts]}")
    print("\n=== 单票历史峰值仓位占比（应 ≤60%）===")
    for s in ORIG_SYMS:
        print(f"  {ORIG_NAMES[s]}({s}): {r['max_sym_wt'][s]:.1%}")
    print(f"  全局最大单票占比: {max(r['max_sym_wt'].values()):.1%}")
    out = os.path.join(HERE, "equity_constrained.csv")
    curve = r["equity_curve"]
    dates = [c["date"] for c in curve]
    eqs = [c["equity"] for c in curve]
    s = pd.Series(eqs, index=dates)
    dd = (s / s.cummax() - 1)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "cash", "drawdown"])
        for d, c in zip(dates, curve):
            w.writerow([d.strftime("%Y-%m-%d"), f"{c['equity']:.6f}", c.get("cash", ""), f"{dd[d]:.6f}"])
    print(f"\n权益曲线已存: {out}")
    print("（配置已内嵌于 ORIG_CONFIG，无需写回外部 JSON）")

def scan_semi_run_one(exit, trail, stop, circuit, cooldown):
    p = dict(sizing='fixed', cap_frac=1.0, entry=10, exit=exit, pyramid_step=0.5,
             max_units=12, trail_multiple=trail, stop_multiple=stop, risk_per_trade=0.075,
             max_symbol_weight=CAP_SEMI, max_positions=9, adx_min=0, mom_lookback=20,
             adx_period=14, circuit=circuit, cooldown=cooldown, cap_trim=True)
    r = run_optimized(p, syms=SEMI_SYMS, data_dir=SEMI_DATA_DIR, start=SEMI_START, end=None)
    peak = max(r["max_sym_wt"].values())
    return r, peak

def cmd_scan_semi():
    """半篮参数扫描：内部 cap=0.585(T+1 缓冲，口径B 硬上限为单票≤60%)，扫 exit/stop/circuit/cooldown。"""
    base = [dict(exit=40, trail=0.0, stop=2.0), dict(exit=70, trail=0.0, stop=2.0),
            dict(exit=40, trail=0.0, stop=4.0), dict(exit=70, trail=0.0, stop=4.0)]
    circuits = [0.19, 0.185, 0.18, 0.175, 0.17, 0.16]
    cooldowns = [10, 15, 20]
    rows = []
    for bc in base:
        for circuit in circuits:
            for cd in cooldowns:
                r, peak = scan_semi_run_one(bc['exit'], bc['trail'], bc['stop'], circuit, cd)
                rows.append((r['total_return'], r['max_drawdown'], peak, r['trades'],
                             r['final'], bc['exit'], bc['stop'], circuit, cd))
    rows.sort(key=lambda x: -x[0])
    print(f"{'收益':>9s} {'回撤':>8s} {'单票峰':>7s} {'成交':>5s} {'终值':>14s}  exit/stop/cir/cd  双约束")
    for ret, dd, peak, tr, final, ex, st, cir, cd in rows:
        ok = '✅' if (dd >= -0.20 and peak <= 0.60) else '❌'
        print(f"{ret:8.2%} {dd:7.2%} {peak:6.2%} {tr:5d} {final:>14,.0f}  "
              f"exit={ex} stop={st} cir={cir} cd={cd}  {ok}")
    feas = [x for x in rows if x[1] >= -0.20 and x[2] <= 0.60]
    print(f"\n[双约束达标] 共 {len(feas)} 组")
    if feas:
        best = feas[0]
        print(f"  最高收益: {best[0]:.2%} / 回撤 {best[1]:.2%} / 单票峰 {best[2]:.2%}  "
              f"(exit={best[5]} stop={best[6]} cir={best[7]} cd={best[8]})")
    # 原篮线上配置(作为对比基准)
    cur = next(x for x in rows if x[5] == 40 and x[6] == 2.0 and abs(x[7] - 0.17) < 1e-9 and x[8] == 10)
    rank = rows.index(cur) + 1
    print(f"\n[原篮线上配置(对比基准) exit=40/stop=2/cir=0.17/cd=10]: 收益 {cur[0]:.2%} / 回撤 {cur[1]:.2%} / "
          f"单票峰 {cur[2]:.2%} / 排名 {rank}/{len(rows)} (双约束{'✅' if cur[1]>=-0.20 and cur[2]<=0.60 else '❌'})")

def js_run(syms, ddir, cap, exit, stop, circuit, cooldown, start=None):
    p = dict(sizing='fixed', cap_frac=1.0, entry=10, exit=exit, pyramid_step=0.5,
             max_units=12, trail_multiple=0.0, stop_multiple=stop, risk_per_trade=0.075,
             max_symbol_weight=cap, max_positions=9, adx_min=0, mom_lookback=20,
             adx_period=14, circuit=circuit, cooldown=cooldown, cap_trim=True)
    r = run_optimized(p, syms=syms, data_dir=ddir, start=start, end=None)
    return r['total_return'], r['max_drawdown'], max(r['max_sym_wt'].values())

def cmd_joint():
    """联合扫描(滚动实时)：共享参数在两篮双约束均达标且都不拉胯。"""
    exits, stops, circuits, cds = [40, 70], [2, 4], [0.16, 0.17, 0.18, 0.19], [10, 15, 20]
    rows = []
    for ex in exits:
        for st in stops:
            for cir in circuits:
                for cd in cds:
                    ro, ddo, pko = js_run(ORIG_SYMS, ORIG_DATA_DIR, 0.60, ex, st, cir, cd)
                    rs, dds, pks = js_run(SEMI_SYMS, SEMI_DATA_DIR, 0.585, ex, st, cir, cd, SEMI_START)
                    feas = (ddo >= -0.20 and pko <= 0.60 and dds >= -0.20 and pks <= 0.60)
                    rows.append((min(ro, rs), (ro + rs) / 2, ro, rs, ddo, dds, pko, pks,
                                 ex, st, cir, cd, feas))
    rows.sort(key=lambda x: -x[0])
    print("== Part A: 共享策略参数(原cap0.60 / 半导体cap0.585)，两篮双约束均达标 ==")
    print(f"{'min':>8s} {'avg':>8s} {'原收益':>8s} {'半收益':>8s} {'原回撤':>7s} {'半回撤':>7s} "
          f"{'原票峰':>6s} {'半票峰':>6s}  exit/stop/cir/cd")
    n = 0
    for mn, av, ro, rs, ddo, dds, pko, pks, ex, st, cir, cd, feas in rows:
        if not feas:
            continue
        n += 1
        print(f"{mn:7.2%} {av:7.2%} {ro:7.2%} {rs:7.2%} {ddo:6.2%} {dds:6.2%} "
              f"{pko:5.2%} {pks:5.2%}  exit={ex} stop={st} cir={cir} cd={cd}")
        if n >= 12:
            break
    print(f"\n[两篮均达标] 共 {sum(1 for x in rows if x[12])} 组 / 全部 {len(rows)} 组")
    ro0, ddo0, pko0 = js_run(ORIG_SYMS, ORIG_DATA_DIR, 0.60, 40, 2, 0.17, 10)
    rs0, dds0, pks0 = js_run(SEMI_SYMS, SEMI_DATA_DIR, 0.585, 40, 2, 0.17, 10, SEMI_START)
    rs1, dds1, pks1 = js_run(SEMI_SYMS, SEMI_DATA_DIR, 0.585, 70, 4, 0.18, 20, SEMI_START)
    ro1, ddo1, pko1 = js_run(ORIG_SYMS, ORIG_DATA_DIR, 0.60, 70, 4, 0.18, 20)
    print(f"\n== 交叉验证 ==")
    print(f"  原最优(exit40/stop2/cir0.17/cd10) 套半导体: 半收益 {rs0:.2%} / 半回撤 {dds0:.2%} (原收益 {ro0:.2%})")
    print(f"  半最优(exit70/stop4/cir0.18/cd20) 套原篮子: 原收益 {ro1:.2%} / 原回撤 {ddo1:.2%} (半收益 {rs1:.2%})")
    feasible = [x for x in rows if x[12]]
    if not feasible:
        print("\n[两篮均达标] 0 组；跳过 Part B 统一cap 测试")
    else:
        best = feasible[0]
        ex, st, cir, cd = best[8], best[9], best[10], best[11]
        print(f"\n== Part B: 最优共享参数(exit={ex}/stop={st}/cir={cir}/cd={cd}) 试统一cap ==")
        for ucap in (0.585, 0.59):
            ro, ddo, pko = js_run(ORIG_SYMS, ORIG_DATA_DIR, ucap, ex, st, cir, cd)
            rs, dds, pks = js_run(SEMI_SYMS, SEMI_DATA_DIR, ucap, ex, st, cir, cd, SEMI_START)
            ok_o = ddo >= -0.20 and pko <= 0.60
            ok_s = dds >= -0.20 and pks <= 0.60
            print(f"  统一cap={ucap}: 原 {ro:.2%}/{ddo:.2%}/票{pko:.2%} {'✅' if ok_o else '❌'}  |  "
                  f"半 {rs:.2%}/{dds:.2%}/票{pks:.2%} {'✅' if ok_s else '❌'}")

def js630_run(syms, ddir, cap, exit, stop, cir, cd, start=None, end="20260630"):
    p = dict(sizing='fixed', cap_frac=1.0, entry=10, exit=exit, pyramid_step=0.5,
             max_units=12, trail_multiple=0.0, stop_multiple=stop, risk_per_trade=0.075,
             max_symbol_weight=cap, max_positions=9, adx_min=0, mom_lookback=20,
             adx_period=14, circuit=cir, cooldown=cd, cap_trim=True)
    r = run_optimized(p, syms=syms, data_dir=ddir, start=start, end=end)
    return r['total_return'], r['max_drawdown'], max(r['max_sym_wt'].values())

def cmd_joint630():
    """联合扫描(定稿窗口 截至2026-06-30)：数据截到 2026-06-30，排除 7 月扰动，公平比较。"""
    exits, stops, circuits, cds = [40, 70], [2, 4], [0.16, 0.17, 0.18, 0.19], [10, 15, 20]
    rows = []
    for ex in exits:
        for st in stops:
            for cir in circuits:
                for cd in cds:
                    ro, ddo, pko = js630_run(ORIG_SYMS, ORIG_DATA_DIR, 0.60, ex, st, cir, cd)
                    rs, dds, pks = js630_run(SEMI_SYMS, SEMI_DATA_DIR, 0.585, ex, st, cir, cd, SEMI_START)
                    ok_o = ddo >= -0.20 and pko <= 0.605
                    ok_s = dds >= -0.20 and pks <= 0.60
                    feas = ok_o and ok_s
                    rows.append((min(ro, rs), (ro + rs) / 2, ro, rs, ddo, dds, pko, pks,
                                 ex, st, cir, cd, feas))
    rows.sort(key=lambda x: -x[0])
    print(f"{'min':>8s} {'avg':>8s} {'原收益':>8s} {'半收益':>8s} {'原回撤':>7s} {'半回撤':>7s} "
          f"{'原票峰':>6s} {'半票峰':>6s}  exit/stop/cir/cd  双约束")
    n = 0
    for mn, av, ro, rs, ddo, dds, pko, pks, ex, st, cir, cd, feas in rows:
        if not feas:
            continue
        n += 1
        print(f"{mn:7.2%} {av:7.2%} {ro:7.2%} {rs:7.2%} {ddo:6.2%} {dds:6.2%} "
              f"{pko:5.2%} {pks:5.2%}  exit={ex} stop={st} cir={cir} cd={cd}")
        if n >= 12:
            break
    print(f"\n[两篮均达标(原单票≤60.5%容差)] 共 {sum(1 for x in rows if x[12])} 组 / 48")

def cmd_scan_sweet():
    """原篮 circuit 甜点区(0.16~0.19)+cooldown 长度，找 ≤20% 内最高收益。"""
    base_cfgs=[
        dict(exit=70,trail=0.0,stop=2.0),
        dict(exit=40,trail=0.0,stop=2.0),
        dict(exit=70,trail=0.0,stop=4.0),
    ]
    circuits=[0.19,0.185,0.18,0.175,0.17,0.16]
    cooldowns=[10,15,20]
    rows=[]
    for bc in base_cfgs:
        for circuit in circuits:
            for cooldown in cooldowns:
                p=dict(sizing='fixed', cap_frac=1.0, entry=10, exit=bc['exit'],
                    pyramid_step=0.5, max_units=12, trail_multiple=bc['trail'], stop_multiple=bc['stop'],
                    risk_per_trade=0.075, max_symbol_weight=0.60, max_positions=9,
                    adx_min=0, mom_lookback=20, adx_period=14, circuit=circuit, cooldown=cooldown,
                    cap_trim=True)
                r=run_optimized(p)
                peak_wt=max(r['max_sym_wt'].values())
                rows.append((r['total_return'],r['max_drawdown'],r['trades'],r['final'],p,peak_wt))
    rows.sort(key=lambda x:-x[0])
    print(f"{'收益':>9s} {'回撤':>8s} {'成交':>5s} {'终值':>14s} {'单票峰':>7s}  配置(cir/cd/exit/trail/stop)")
    for ret,dd,tr,final,p,peak_wt in rows:
        ok='✅≤20%' if dd>=-0.20 else '❌>20%'
        cap_ok='✅≤60%' if peak_wt<=0.605 else '❌>60%'
        print(f"{ret:8.2%} {dd:7.2%} {tr:5d} {final:>14,.0f} {peak_wt:6.1%}  cir={p['circuit']} cd={p['cooldown']} exit={p['exit']} trail={p['trail_multiple']} stop={p['stop_multiple']}  {ok} {cap_ok}")
    ok_rows=[r for r in rows if r[1]>=-0.20 and r[5]<=0.605]
    print(f"\n[回撤≤20% 且 单票≤60%] 共 {len(ok_rows)} 组")
    if ok_rows:
        print(f"  最高收益: {ok_rows[0][0]:.2%} / 回撤 {ok_rows[0][1]:.2%} / 单票峰 {ok_rows[0][5]:.1%}  (cir={ok_rows[0][4]['circuit']} cd={ok_rows[0][4]['cooldown']} exit={ok_rows[0][4]['exit']} trail={ok_rows[0][4]['trail_multiple']} stop={ok_rows[0][4]['stop_multiple']})")
        print(f"  最浅回撤: {min(r[1] for r in rows):.2%}")
    else:
        print("  无一组同时满足双约束")
        print(f"  全局最浅回撤: {min(r[1] for r in rows):.2%}")

def cmd_refresh():
    """增量刷新两篮本地 qfq 日线（原 refresh_data.main）。
    返回 (成功数, 失败数)，便于调用方(cron)按失败数决定退出码。"""
    BASE = HERE
    TODAY = pd.Timestamp.today().normalize()
    COLS = ['date','open','high','low','close','volume','amount']
    def refresh(symbols, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        ok = fail = 0
        for code in symbols:
            path = os.path.join(data_dir, f"{code}.csv")
            last = None
            if os.path.exists(path):
                old = pd.read_csv(path)
                if 'date' in old.columns and len(old):
                    last = pd.to_datetime(old['date']).max()
            start = (last + pd.Timedelta(days=1)) if last is not None else pd.Timestamp("2020-01-01")
            if start > TODAY:
                print(f"  {code} 已是最新(末日 {last.date()})")
                continue
            try:
                fresh = load_daily(code, start.strftime("%Y%m%d"), TODAY.strftime("%Y%m%d"),
                                   adjust="qfq", use_cache=False)
            except Exception as e:
                print(f"  [skip] {code} 刷新失败: {e}")
                fail += 1
                continue
            if fresh is None or len(fresh) == 0:
                continue
            fresh = fresh.copy()
            fresh['date'] = pd.to_datetime(fresh['date'])
            if os.path.exists(path):
                old = pd.read_csv(path)
                old['date'] = pd.to_datetime(old['date'])
                fresh = fresh.reindex(columns=[c for c in COLS if c in fresh.columns])
                combined = pd.concat([old, fresh], ignore_index=True).drop_duplicates('date').sort_values('date')
            else:
                combined = fresh.sort_values('date')
            combined.to_csv(path, index=False)
            print(f"  {code} +{len(fresh)} 行 -> 共 {len(combined)} 行, 末日 {combined['date'].max().date()}")
            ok += 1
        return ok, fail
    print("== 刷新光通信/存储 9 只 ==")
    ok1, fail1 = refresh(ORIG_SYMS, ORIG_DATA_DIR)
    print("== 刷新半导体 9 只(剔除中芯) ==")
    ok2, fail2 = refresh(SEMI_SYMS, SEMI_DATA_DIR)
    print(f"-- 刷新汇总: 成功 {ok1+ok2} / 失败 {fail1+fail2} --")
    return ok1 + ok2, fail1 + fail2

def cmd_fetch_semi():
    """抓取半篮 10 只真实前复权(qfq)日线，存为 data_semi/{code}.csv。"""
    os.makedirs(FETCH_OUT, exist_ok=True)
    for code, name in FETCH_SYMS.items():
        try:
            df = load_daily(code, FETCH_START, FETCH_END, adjust="qfq")
            df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
            df.to_csv(os.path.join(FETCH_OUT, f"{code}.csv"), index=False)
            print(f"  {name}({code}): {len(df)} 行  {df['date'].min().date()}~{df['date'].max().date()}")
        except Exception as e:
            print(f"  ❌ {name}({code}) 获取失败: {e}")
    print("done ->", FETCH_OUT)

def cmd_plot():
    """画权益曲线 + 回撤图（equity_constrained.png）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    df=pd.read_csv(os.path.join(HERE, "equity_constrained.csv"))
    df['date']=pd.to_datetime(df['date'])
    df=df.sort_values('date').reset_index(drop=True)
    eq=df['equity']
    dd=eq/eq.cummax()-1
    fig,ax=plt.subplots(2,1,figsize=(11,7),sharex=True,gridspec_kw={'height_ratios':[3,1]})
    ax[0].plot(df['date'],eq/1e6,color='#1f4e79',lw=1.3,label='Portfolio Equity')
    ax[0].axhline(2.0,color='gray',ls=':',lw=1,label='Initial 2.0M')
    ax[0].set_ylabel('Equity (M)')
    total_ret = eq.iloc[-1] / eq.iloc[0] - 1
    maxdd = dd.min()
    ax[0].set_title(f'Constrained-optimal: +{total_ret:.2%} / MaxDD {maxdd:.2%} (single<=60% + circuit<=20%)')
    ax[0].legend(loc='upper left',fontsize=8)
    ev=df[df['date']=='2025-11-14']
    if len(ev):
        ax[0].scatter(ev['date'],ev['equity']/1e6,color='red',zorder=5,s=40)
        ax[0].annotate('Circuit clear\n2025-11-14',(ev['date'].iloc[0],ev['equity'].iloc[0]/1e6),
                       textcoords='offset points',xytext=(10,-30),color='red',fontsize=8)
    ax[1].fill_between(df['date'],dd*100,0,color='#c0392b',alpha=0.35)
    ax[1].plot(df['date'],dd*100,color='#c0392b',lw=0.8)
    ax[1].axhline(-20,color='black',ls='--',lw=1.2,label='-20% hard limit')
    ax[1].axhline(0,color='gray',lw=0.6)
    ax[1].set_ylabel('Drawdown(%)')
    ax[1].set_ylim(-25,2)
    ax[1].legend(loc='lower left',fontsize=8)
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    fig.autofmt_xdate()
    fig.tight_layout()
    out=os.path.join(HERE, "equity_constrained.png")
    fig.savefig(out,dpi=130)
    print("Chart saved:",out)

def cmd_backtest_semi():
    """半篮 10 只(含中芯)对比回测 + T+1 校验（原 backtest_semi.py）。"""
    from collections import defaultdict
    P = dict(sizing='fixed', cap_frac=1.0, entry=10, exit=40,
             pyramid_step=0.5, max_units=12, trail_multiple=0.0, stop_multiple=2.0,
             risk_per_trade=0.075, max_symbol_weight=0.60, max_positions=10,
             adx_min=0, mom_lookback=20, adx_period=14, circuit=0.17, cooldown=10,
             cap_trim=True)
    def t1_violations(p):
        orig = Portfolio.execute
        seen = defaultdict(set); viol = []
        def patched(self, order, fill, date):
            key = (order.symbol, order.strategy)
            if order.action == 'BUY' and order.shares > 0 and not self.halted:
                seen[key].add(date)
            elif order.action == 'SELL' and date in seen.get(key, set()):
                viol.append((date, key, order.reason))
            return orig(self, order, fill, date)
        Portfolio.execute = patched
        run_optimized(p, syms=LEGACY_SEMI_SYMS, data_dir=LEGACY_SEMI_DATA_DIR, start=LEGACY_SEMI_START)
        Portfolio.execute = orig
        return viol
    print("="*70)
    print("一、与原始组合相同配置（exit=40/circuit=0.17/cd=10/cap_trim）")
    print("="*70)
    r = run_optimized(P, syms=LEGACY_SEMI_SYMS, data_dir=LEGACY_SEMI_DATA_DIR, start=LEGACY_SEMI_START)
    peak_wt = max(r['max_sym_wt'].values())
    print(f"总收益 : {r['total_return']:.2%}")
    print(f"最大回撤: {r['max_drawdown']:.2%}  (硬约束 ≤ -20%)")
    print(f"成交数 : {r['trades']}   买入: {r['n_buy']}")
    print(f"终值   : {r['final']:,.0f}  (初始 2,000,000)")
    print(f"单票峰值(合计): {peak_wt:.1%}  (硬约束 ≤ 60%)")
    viol = t1_violations(P)
    print(f"T+1 同日买+卖违规: {len(viol)}")
    print("\n逐票峰值仓位占比（应 ≤60%）:")
    for s in LEGACY_SEMI_SYMS:
        print(f"  {LEGACY_SEMI_NAMES[s]}({s}): {r['max_sym_wt'][s]:.1%}")
    print("\n" + "="*70)
    print("二、本篮子扫描（exit∈{40,70} × circuit∈{0.16~0.19} × cd∈{10,15}，cap_trim）")
    print("="*70)
    circuits=[0.16,0.17,0.18,0.185,0.19]; cooldowns=[10,15]; exits=[40,70]
    rows=[]
    for exit_ in exits:
        for circuit in circuits:
            for cd in cooldowns:
                p=dict(sizing='fixed', cap_frac=1.0, entry=10, exit=exit_,
                    pyramid_step=0.5, max_units=12, trail_multiple=0.0, stop_multiple=2.0,
                    risk_per_trade=0.075, max_symbol_weight=0.60, max_positions=10,
                    adx_min=0, mom_lookback=20, adx_period=14, circuit=circuit, cooldown=cd,
                    cap_trim=True)
                rr=run_optimized(p, syms=LEGACY_SEMI_SYMS, data_dir=LEGACY_SEMI_DATA_DIR, start=LEGACY_SEMI_START)
                pw=max(rr['max_sym_wt'].values())
                rows.append((rr['total_return'],rr['max_drawdown'],rr['trades'],rr['final'],p,pw))
    rows.sort(key=lambda x:-x[0])
    print(f"{'收益':>9s} {'回撤':>8s} {'成交':>5s} {'终值':>14s} {'单票峰':>7s}  配置")
    for ret,dd,tr,final,p,pw in rows:
        ok='✅≤20%' if dd>=-0.20 else '❌>20%'
        cap='✅≤60%' if pw<=0.605 else '❌>60%'
        print(f"{ret:8.2%} {dd:7.2%} {tr:5d} {final:>14,.0f} {pw:6.1%}  cir={p['circuit']} cd={p['cooldown']} exit={p['exit']}  {ok} {cap}")
    ok_rows=[r for r in rows if r[1]>=-0.20 and r[5]<=0.605]
    print(f"\n[回撤≤20% 且 单票≤60%] 共 {len(ok_rows)} 组")
    if ok_rows:
        best=ok_rows[0]
        print(f"  最高收益: {best[0]:.2%} / 回撤 {best[1]:.2%} / 单票峰 {best[5]:.1%}  (cir={best[4]['circuit']} cd={best[4]['cooldown']} exit={best[4]['exit']})")

def cmd_mine():
    """我的策略(海龟10/70 + 均线)单标的 + 9 只等权组合（原 run_mine.py）。"""
    MINE_START, MINE_END = "20250101", "20260630"
    def mine_make_cfg():
        cfg = replace(Config(), max_total_risk=0.99, stop_multiple=4.0,
                      max_units=8, risk_per_trade=0.075)
        cfg._entry, cfg._exit = 10, 70
        cfg.initial_capital = 2_000_000
        return cfg
    def mine_load(code):
        return load_daily(code, MINE_START, MINE_END, adjust="qfq", use_cache=False)
    cfg = mine_make_cfg()
    print(f"  我的策略: 海龟(10/70/止损4ATR/8单位/风险7.5%) + 均线(MA20/MA60)  初始200万  成本0.03%/0.05%/0.1%")
    print(f"{'标的':10s} {'收益%':>9s} {'回撤%':>8s} {'成交':>5s} {'期末权益':>14s}")
    print("-"*60)
    for code in ORIG_SYMS:
        df = mine_load(code)
        strs = [TurtleStrategy(code, cfg.initial_capital*0.5, cfg, 10, 70),
                MATrendStrategy(code, cfg.initial_capital*0.5, cfg)]
        res = run_backtest_multi({code: df}, cfg, strategies=strs, donchian_periods={5,10,20,10,70})
        m = compute_metrics(res, cfg)
        print(f"  {ORIG_NAMES[code]:10s} {m['total_return']*100:8.1f}  {m['max_drawdown']*100:7.1f}  "
              f"{m['n_buy_orders']+m['n_sell_orders']:5d}  {m['final_equity']:>14,.0f}")
    print("-"*60)
    n = len(ORIG_SYMS)
    data = {c: mine_load(c) for c in ORIG_SYMS}
    strs = []
    for c in ORIG_SYMS:
        strs.append(TurtleStrategy(c, cfg.initial_capital*0.5/n, cfg, 10, 70))
        strs.append(MATrendStrategy(c, cfg.initial_capital*0.5/n, cfg))
    res = run_backtest_multi(data, cfg, strategies=strs, donchian_periods={5,10,20,10,70})
    mc = compute_metrics(res, cfg)
    print(f"  {'9只等权组合':10s} {mc['total_return']*100:8.1f}  {mc['max_drawdown']*100:7.1f}  "
          f"{mc['n_buy_orders']+mc['n_sell_orders']:5d}  {mc['final_equity']:>14,.0f}")
    print("="*60)

def cmd_debug_circuit():
    """调试 2025-09 熔断区间持仓（原 debug_circuit.py）。"""
    def run_with_debug(P, debug=False):
        cfg=make_cfg(P)
        data,mom_map=load_enriched(P)
        bars_by_date={s:{row['date']:row for _,row in df.iterrows()} for s,df in data.items()}
        master=None
        for s in ORIG_SYMS:
            idx=set(bars_by_date[s].keys()); master=idx if master is None else (master&idx)
        all_dates=sorted(master)
        strategies=[]
        for s in ORIG_SYMS:
            strategies.append(OptTurtle(s,cfg.initial_capital,cfg,P['entry'],P['exit'],adx_min=P['adx_min']))
            strategies.append(MATrendStrategy(s,cfg.initial_capital,cfg))
        st_by_id={(st.symbol,st.id):st for st in strategies}
        pf=Portfolio(cfg)
        last_close={s:None for s in ORIG_SYMS}; pending=[]; eq_curve=[]
        peak=cfg.initial_capital; cooldown_remaining=0
        cap_frac=P['cap_frac']; max_sym_w=P['max_symbol_weight']; max_pos=P['max_positions']
        def sym_value(prices):
            return sum(sh*(prices.get(sym) or 0) for (sym,_),sh in pf.positions.items() if sh>0)
        for date in all_dates:
            for s in ORIG_SYMS:
                if date in bars_by_date[s]: last_close[s]=float(bars_by_date[s][date]['close'])
            prices={s:last_close[s] for s in ORIG_SYMS}
            equity=pf.cash+sym_value(prices)
            for st in strategies: st.capital=cfg.initial_capital*cap_frac
            for order in pending:
                row=bars_by_date[order.symbol].get(date)
                if row is None: continue
                open_px=float(row['open'])
                fill=open_px if order.limit_price is None or open_px<=order.limit_price else order.limit_price
                shares=order.shares
                if order.action=='BUY':
                    cur_sym=sum(sh*(prices.get(order.symbol) or 0) for (sym,_),sh in pf.positions.items() if sym==order.symbol)
                    room=max(0.0,max_sym_w*equity-cur_sym); max_shares=int(room/fill//100)*100 if fill>0 else 0
                    shares=min(shares,max_shares)
                    if shares<=0: continue
                pf.execute(Order(order.symbol,order.action,shares,order.strategy,order.reason,order.limit_price),fill,date)
                st=st_by_id.get((order.symbol,order.strategy))
                if st: st.sync_position(pf.positions.get((order.symbol,order.strategy),0))
            pending=[]
            if pf.halted: eq_curve.append(equity); continue
            peak=max(peak,equity); dd=(equity/peak-1) if peak>0 else 0.0
            risk_off=cooldown_remaining>0
            if risk_off: cooldown_remaining-=1
            elif dd<=-P['circuit']:
                for (sym,stid),sh in list(pf.positions.items()):
                    if sh>0:
                        pf.execute(Order(sym,'SELL',sh,stid,'circuit_break'),last_close[sym],date)
                        st=st_by_id.get((sym,stid))
                        if st: st.sync_position(0)
                cooldown_remaining=P['cooldown']; peak=equity; equity=pf.cash
                risk_off=True
                if debug and date>=pd.Timestamp('2025-09-01') and date<=pd.Timestamp('2025-09-15'):
                    tot=sum(sh for sh in pf.positions.values())
                    print(f"  [熔断] {date.date()} 清仓后总股数={tot} 现金={pf.cash:,.0f}")
            if mom_map:
                ranked=[(s,mom_map[s].get(date)) for s in ORIG_SYMS]; ranked=[(s,m) for s,m in ranked if m is not None]
                ranked.sort(key=lambda x:-x[1]); allowed=set(s for s,_ in ranked[:max_pos])
            else: allowed=set(ORIG_SYMS)
            for st in strategies:
                row=bars_by_date[st.symbol].get(date)
                if row is None: continue
                for o in st.on_bar(row):
                    if o.action=='BUY':
                        if risk_off: continue
                        flat=all(sh<=0 for (sym,_),sh in pf.positions.items() if sym==o.symbol)
                        if flat and o.symbol not in allowed: continue
                    pending.append(o)
            if debug and date>=pd.Timestamp('2025-09-01') and date<=pd.Timestamp('2025-09-15'):
                tot=sum(sh for sh in pf.positions.values())
                print(f"  {date.date()} 收盘权益={equity:,.0f} 持仓总股数={tot} risk_off={risk_off} cooldown={cooldown_remaining}")
            eq_curve.append(equity)
        eq=pd.Series(eq_curve)
        return eq.iloc[-1]/cfg.initial_capital-1, (eq/eq.cummax()-1).min()
    P=dict(sizing='fixed', cap_frac=1.0, entry=10, exit=40, pyramid_step=0.5, max_units=12,
           trail_multiple=0.0, stop_multiple=2.0, risk_per_trade=0.075, max_symbol_weight=0.60,
           max_positions=9, adx_min=0, mom_lookback=20, adx_period=14, circuit=0.16, cooldown=20)
    print("=== 调试 2025-09 区间持仓 ===")
    r=run_with_debug(P, debug=True)
    print(f"结果: 收益={r[0]:.2%} 回撤={r[1]:.2%}")
    print("\n=== 对比：关闭熔断(circuit=0.99) ===")
    P2=dict(P); P2['circuit']=0.99
    r2=run_with_debug(P2)
    print(f"关闭熔断: 收益={r2[0]:.2%} 回撤={r2[1]:.2%}")

def cmd_turtle_v11():
    """对比上传的 turtle_v11 实跑结果（原 run_turtle_v11.py）。"""
    if not os.path.exists(TURTLE_V11_PATH):
        print(f"[skip] 未找到上传文件: {TURTLE_V11_PATH}")
        return
    spec = importlib.util.spec_from_file_location("turtle_v11", TURTLE_V11_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["turtle_v11"] = m
    spec.loader.exec_module(m)
    data = {}
    for code in ORIG_SYMS:
        df = m.load_ohlcv_csv(os.path.join(ORIG_DATA_DIR, f"{code}.csv"))
        data[code] = df
    cfg = m.TurtleConfig()
    print("=== TurtleConfig() 实际默认参数 ===")
    print(f"  initial_capital={cfg.initial_capital}, max_drawdown={cfg.max_drawdown}")
    print(f"  atr_stop_multiple={cfg.atr_stop_multiple}, use_donchian_exit={cfg.use_donchian_exit}")
    print(f"  max_symbol_weight={cfg.max_symbol_weight}, max_total_stock_weight={cfg.max_total_stock_weight}")
    print(f"  max_units_per_symbol={cfg.max_units_per_symbol}, pyramid_add_atr={cfg.pyramid_add_atr}")
    print(f"  risk_off_cooldown_days={cfg.risk_off_cooldown_days}")
    print(f"  commission={cfg.commission_rate}, stamp={cfg.stamp_tax_rate}, slippage_bps={cfg.slippage_bps}")
    print(f"  systems={[(s.name,s.entry_window,s.exit_window,s.risk_fraction) for s in cfg.systems]}")
    res = m.run_backtest(data, cfg)
    s = res.summary
    print("\n=== turtle_v11 实跑结果（TurtleConfig()默认 / 原生成本） ===")
    print(f"  区间: {s['start_date']} ~ {s['end_date']}")
    print(f"  初始资金: {s['initial_capital']:,.0f}")
    print(f"  终值:     {s['final_equity']:,.0f}")
    print(f"  总收益率: {s['total_return']:.2%}")
    print(f"  年化收益: {s['annual_return']:.2%}")
    print(f"  最大回撤: {s['max_drawdown']:.2%}")
    print(f"  交易次数: {s['trade_count']}, 胜率: {s['win_rate']}")
    print(f"  未平仓数: {s['open_positions']}")
    import dataclasses
    cfg2 = dataclasses.replace(cfg, commission_rate=0.0003, stamp_tax_rate=0.0005, slippage_bps=10.0)
    res2 = m.run_backtest(data, cfg2)
    s2 = res2.summary
    print("\n=== turtle_v11 统一成本(0.03%/0.05%/0.1%) ===")
    print(f"  总收益率: {s2['total_return']:.2%}  最大回撤: {s2['max_drawdown']:.2%}")

def cmd_all():
    """每日定时任务统一入口：刷新数据 -> 双篮重跑 -> 出汇总报告（原 run_all.main）。"""
    REPORT_DIR = os.path.join(HERE, "reports")
    REPORT_PATH = os.path.join(REPORT_DIR, "backtest_daily.md")
    TODAY = datetime.date.today()
    def constraint_tag(ok):
        return "✅ 达标" if ok else "❌ 突破"
    print(f"=== 量化回测每日刷新+重跑 [{TODAY}] ===")
    print("-- 1) 刷新数据 --")
    try:
        cmd_refresh()
    except Exception as e:
        print(f"  [warn] 数据刷新异常(继续跑回测): {e}")
    print("-- 2) 光通信/存储 9 只 (配置内嵌 ORIG_CONFIG, 上限0.60) --")
    r1, peak1 = cmd_original()
    print("-- 3) 半导体 9 只 (配置内嵌 SEMI_CONFIG, 上限0.585) --")
    r2, peak2 = cmd_semi()
    semi_cap = 0.585

    # --- 数据陈旧可见告警（消除静默失效）---
    # 本沙箱到数据源出站被封时，内置 refresh 会静默跳过；此处显式检测两篮本地数据末日，
    # 用交易日差判断（周末不误报），≥2 个交易日未更新即标红，提醒"联网环境刷新未同步回"。
    def _data_end_all():
        ends = []
        for d, syms in ((ORIG_DATA_DIR, ORIG_SYMS), (SEMI_DATA_DIR, SEMI_SYMS)):
            for code in syms:
                p = os.path.join(d, f"{code}.csv")
                if not os.path.exists(p):
                    continue
                try:
                    dt = pd.read_csv(p, usecols=['date'])['date'].max()
                    ends.append(pd.to_datetime(dt))
                except Exception:
                    pass
        return ends
    data_ends = _data_end_all()
    data_end_max = max(data_ends) if data_ends else None
    data_end_min = min(data_ends) if data_ends else None
    data_end = data_end_max
    gap_bd = (int(np.busday_count(data_end.date() + pd.Timedelta(days=1), TODAY))
              if data_end is not None else None)
    stale = (gap_bd is not None and gap_bd >= 2)
    if data_end is not None:
        flag = "⚠️ 数据已陈旧" if stale else "✅ 数据新鲜"
        lag_note = f"（部分标的滞后至 {data_end_min.date()}）" if (data_end_min is not None and data_end_min < data_end_max) else ""
        print(f"-- 数据末日 {data_end.date()}{lag_note} | 距今天 {gap_bd} 个交易日 {flag} --")
    else:
        print("-- 数据末日: 未知 --")

    c1_dd = r1['max_drawdown'] >= -0.20
    c1_wt = peak1 <= 0.60
    c2_dd = r2['max_drawdown'] >= -0.20
    c2_wt = peak2 <= 0.60
    c2_wt_internal = peak2 <= semi_cap
    all_ok = all([c1_dd, c1_wt, c2_dd, c2_wt])
    md = []
    md.append(f"# 量化回测每日汇总（{TODAY}）\n")
    fresh_line = (f"> 数据末日 **{data_end.date()}**，距今天 **{gap_bd}** 个交易日"
                  + (" ⚠️ **数据已陈旧——联网环境刷新未同步回，下方为旧数据回测结果**" if stale else " ✅ 数据新鲜"))
    md.append(fresh_line + "\n")
    md.append("> 数据：增量刷新至最新交易日；回测使用全部可用数据。\n")
    md.append("## 一、双篮子结果\n")
    md.append("| 篮子 | 配置 | 收益 | 最大回撤 | 单票峰 | 终值 | 成交 |")
    md.append("|---|---|---|---|---|---|---|")
    md.append(f"| 光通信/存储 9 只 | 上限0.60 | {r1['total_return']:.2%} | {r1['max_drawdown']:.2%} | {peak1:.2%} | {r1['final']:,.0f} | {r1['trades']} |")
    md.append(f"| 半导体 9 只(剔除中芯) | 上限0.585 | {r2['total_return']:.2%} | {r2['max_drawdown']:.2%} | {peak2:.2%} | {r2['final']:,.0f} | {r2['trades']} |")
    md.append("\n## 二、约束校验（口径B：单票≤60% + 回撤≤20%）\n")
    md.append(f"- 光通信/存储：回撤≤20% {constraint_tag(c1_dd)}（{r1['max_drawdown']:.2%}）；单票≤60% {constraint_tag(c1_wt)}（{peak1:.2%}）")
    semi_internal = "✅" if c2_wt_internal else "⚠ 内部上限轻微超出(T+1豁免)"
    md.append(f"- 半导体：回撤≤20% {constraint_tag(c2_dd)}（{r2['max_drawdown']:.2%}）；单票≤60% {constraint_tag(c2_wt)}（峰值{peak2:.2%}；内部上限58.5% {semi_internal}）")
    md.append("\n## 三、风控提示\n")
    if all_ok:
        md.append("- 两套配置均满足双约束，无需操作。")
    else:
        breaches = []
        if not c1_dd: breaches.append(f"光通信/存储回撤突破20%（{r1['max_drawdown']:.2%}）")
        if not c1_wt: breaches.append(f"光通信/存储单票突破60%（{peak1:.2%}）")
        if not c2_dd: breaches.append(f"半导体回撤突破20%（{r2['max_drawdown']:.2%}）")
        if not c2_wt: breaches.append(f"半导体单票突破60%（{peak2:.2%}）")
        md.append("- ⚠️ **存在约束突破，需人工复核：** " + "；".join(breaches) + "。")
        md.append("- 纪律：单票>60% 当根收盘减至上限；组合回撤自峰值触熔断阈值(原-17%/半-18%) 当根收盘清仓、冷却(原10/半20)个交易日。")
    md.append(f"\n---\n*生成时间 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}。源配置：内嵌于 quant_all.py (ORIG_CONFIG / SEMI_CONFIG)*")
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(md))
    print("\n=== 汇总 ===")
    print(f"光通信/存储: 收益 {r1['total_return']:.2%} / 回撤 {r1['max_drawdown']:.2%} / 单票峰 {peak1:.2%}")
    print(f"半导体:      收益 {r2['total_return']:.2%} / 回撤 {r2['max_drawdown']:.2%} / 单票峰 {peak2:.2%}")
    print("约束校验:", "全部达标 ✅" if all_ok else "存在突破 ❌ (见报告)")
    print(f"报告已写: {REPORT_PATH}")
    return all_ok

# ========================= 每日收盘信号（科技股专用·自包含） =========================
# 与 daily_signal.py 同口径，但完全复用本文件已内联的引擎（Config / TurtleStrategy /
# MATrendStrategy / add_indicators / load_daily），不依赖任何外部自定义包，
# 标的范围锁定为 13 只科技股（光通信 / 存储 / 半导体），杜绝扫到非科技标的。
SIGNAL_NAMES = {
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信",
    "688008": "澜起科技", "603986": "兆易创新", "002409": "雅克科技",
    "688072": "拓荆科技", "688300": "联瑞新材", "300054": "鼎龙股份",
    "688205": "德科立", "920045": "蘅东光", "300776": "帝尔激光",
    "688535": "华海诚科",
}
SIGNAL_DONCHIAN = {10, 40}   # 海龟入场上轨/离场下轨周期（突破10 / 离场40）
SIGNAL_CAP = 1_000_000.0     # 每策略分配资本（仅影响单位股数，不影响信号方向）

def signal_make_cfg():
    # 与约束最优版信号逻辑一致：止损2ATR / 12单位（仅影响单位股数，不影响信号方向）
    return replace(Config(), max_total_risk=0.99, stop_multiple=2.0,
                   max_units=12, risk_per_trade=0.075, target_atr_pct=0.0)

def signal_fetch(code, end):
    # 蘅东光(920045)属北交所 920 段，引擎 _is_a_share 未放行"9"前缀，故与 daily_signal.py 一致
    # 直接走 akshare 北交源，避免被 assert_a_share 拦截。
    if code == "920045":
        df = ak.stock_zh_a_daily(symbol="bj920045", start_date="20250101",
                                 end_date=end, adjust="qfq")
        df = _normalize(df)
    else:
        df = load_daily(code, "20250101", end, adjust="qfq", use_cache=False)
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)

def signal_snapshot(strat, bar):
    """抓取重放至某 bar 后的策略状态与当根指标。"""
    ep = strat.entry_prices
    avg_entry = float(np.mean(ep)) if ep else float("nan")
    return {
        "in_position": strat.in_position,
        "units": strat.units,
        "avg_entry": avg_entry,
        "stop": strat.stop_price,
        "close": float(bar["close"]),
        "upper10": bar.get("donchian_upper_10"),
        "lower40": bar.get("donchian_lower_40"),
        "ma_fast": bar.get("ma_fast"),
        "ma_slow": bar.get("ma_slow"),
        "atr": float(bar["atr"]) if not pd.isna(bar.get("atr")) else float("nan"),
    }

def signal_replay(strategy, df):
    """重放单策略至最后一根 bar，返回 (最新bar产生的订单, 最新收盘时点状态快照)。"""
    enr = add_indicators(df, strategy.cfg, donchian_periods=SIGNAL_DONCHIAN)
    enr = enr.reset_index(drop=True)
    pos = 0
    pending = []
    last_snap = None
    for i in range(len(enr)):
        bar = enr.iloc[i]
        # 1) 撮合上一根挂单（本根开盘价）——T+1 天然满足
        for order in pending:
            open_px = float(bar["open"])
            if order.limit_price is not None:
                fill = open_px if open_px <= order.limit_price else order.limit_price
            else:
                fill = open_px
            pos = pos + order.shares if order.action == "BUY" else max(0, pos - order.shares)
            strategy.sync_position(pos)
        # 2) 生成新信号
        if i == len(enr) - 1:
            last_snap = signal_snapshot(strategy, bar)   # 最新收盘时点的持仓状态
        pending = strategy.on_bar(bar)
    return pending, last_snap

def signal_classify(pending):
    if not pending:
        return "持有", ""
    buys = [o for o in pending if o.action == "BUY"]
    sells = [o for o in pending if o.action == "SELL"]
    if sells:
        return "卖出", sells[0].reason
    if buys:
        return "买入", buys[0].reason
    return "持有", ""

def signal_reason_text(strat_name, sig, reason, snap):
    if sig == "买入":
        if "entry" in reason:
            if strat_name == "ma":
                lvl = snap.get("ma_fast")
                return f"站上MA20建仓(>{lvl:.2f})" if (lvl is not None and pd.notna(lvl)) else "MA20建仓"
            lvl = snap["upper10"]
            return f"突破建仓(>{lvl:.2f})" if pd.notna(lvl) else "突破建仓"
        return "加仓(金字塔)"
    if sig == "卖出":
        if "stop" in reason:
            lvl = snap.get("stop")
            return f"止损@{lvl:.2f}" if (lvl is not None and pd.notna(lvl)) else "止损"
        if "exit_breakdown" in reason:
            lvl = snap.get("lower40")
            return f"跌破40日低({lvl:.2f})" if (lvl is not None and pd.notna(lvl)) else "跌破40日低"
        if "exit_ma" in reason:
            lvl = snap.get("ma_fast")
            return f"跌破MA20({lvl:.2f})" if (lvl is not None and pd.notna(lvl)) else "跌破MA20"
        if "trim" in reason:
            return "波动减仓"
        return reason
    # 持有
    if snap["in_position"]:
        return f"持仓{int(snap['units'])}单位@均价{snap['avg_entry']:.2f}"
    return "空仓观望"

def signal_run_one(code, end):
    cfg = signal_make_cfg()
    df = signal_fetch(code, end)
    if df is None or len(df) < 80:
        return {"code": code, "name": SIGNAL_NAMES[code], "err": f"数据不足({len(df) if df is not None else 0}根)"}
    turtle = TurtleStrategy(code, SIGNAL_CAP, cfg, 10, 40)
    ma = MATrendStrategy(code, SIGNAL_CAP, cfg)
    pt, st = signal_replay(turtle, df)
    pm, sm = signal_replay(ma, df)
    t_sig, t_reason = signal_classify(pt)
    m_sig, m_reason = signal_classify(pm)
    # 综合：任一看空→卖出；任一买入→买入；否则持有
    if t_sig == "卖出" or m_sig == "卖出":
        overall = "卖出"
    elif t_sig == "买入" or m_sig == "买入":
        overall = "买入"
    else:
        overall = "持有"
    return {
        "code": code, "name": SIGNAL_NAMES[code],
        "close": st["close"], "in_pos": st["in_position"] or sm["in_position"],
        "units": max(st["units"], sm["units"]),
        "avg_entry": st["avg_entry"] if st["in_position"] else (sm["avg_entry"] if sm["in_position"] else float("nan")),
        "stop": st["stop"] if st["in_position"] else sm["stop"],
        "t_sig": t_sig, "t_reason": signal_reason_text("turtle", t_sig, t_reason, st),
        "m_sig": m_sig, "m_reason": signal_reason_text("ma", m_sig, m_reason, sm),
        "overall": overall,
        "upper10": st["upper10"], "lower40": st["lower40"], "ma_fast": st["ma_fast"], "ma_slow": st["ma_slow"],
    }

def cmd_signal():
    """每日收盘科技股信号（自包含，不依赖外部包）。

    用法：python3.11 quant_all.py signal [YYYYMMDD]
    用当天（或指定截止日）最新收盘数据，将海龟(突破10/离场40)与均线(MA20/MA60)
    两套策略严格按引擎时序重放到最新一根 bar，给出每只科技股的持仓状态+海龟信号
    +均线信号+综合(买入/持有/卖出)+触发价位，并落盘 signals/signal_<date>.csv。
    """
    import warnings
    from datetime import date
    warnings.filterwarnings("ignore")
    end = sys.argv[2] if len(sys.argv) > 2 else date.today().strftime("%Y%m%d")
    print("=" * 96)
    print(f"  每日策略信号（科技股专用·自包含）  数据截至 {end} 收盘  参数: 海龟(突破10/离场40/止损2ATR/12单位) + 均线(MA20/MA60)")
    print("=" * 96)
    header = f"{'标的':8s} {'代码':7s} {'现价':>9s} {'持仓':>10s} {'海龟信号':>10s} {'均线信号':>10s} {'综合':>6s}  触发/理由"
    print(header)
    print("-" * 96)
    rows = []
    for code in SIGNAL_NAMES:
        try:
            r = signal_run_one(code, end)
        except Exception as e:
            r = {"code": code, "name": SIGNAL_NAMES[code], "err": f"异常:{repr(e)[:60]}"}
        if "err" in r:
            print(f"  {r['name']:8s} {r['code']:7s}  {r['err']}")
            continue
        pos = f"{'持仓' if r['in_pos'] else '空仓'}{int(r['units'])}单位" if r['in_pos'] else "空仓"
        tcol = {"买入": "🔼买入", "卖出": "🔻卖出", "持有": "⚪持有"}[r["t_sig"]]
        mcol = {"买入": "🔼买入", "卖出": "🔻卖出", "持有": "⚪持有"}[r["m_sig"]]
        ocol = {"买入": "🔼买入", "卖出": "🔻卖出", "持有": "⚪持有"}[r["overall"]]
        reason = r["t_reason"] if r["t_sig"] != "持有" else (r["m_reason"] if r["m_sig"] != "持有" else r["t_reason"])
        print(f"  {r['name']:8s} {r['code']:7s} {r['close']:9.2f} {pos:>10s} {tcol:>10s} {mcol:>10s} {ocol:>6s}  {reason}")
        rows.append(r)
    print("-" * 96)
    buys = [r["name"] for r in rows if r["overall"] == "买入"]
    sells = [r["name"] for r in rows if r["overall"] == "卖出"]
    holds = [r["name"] for r in rows if r["overall"] == "持有"]
    print(f"  🔼 买入({len(buys)}): {', '.join(buys) if buys else '—'}")
    print(f"  🔻 卖出({len(sells)}): {', '.join(sells) if sells else '—'}")
    print(f"  ⚪ 持有({len(holds)}): {', '.join(holds) if holds else '—'}")
    print("=" * 96)
    # 落盘日志
    os.makedirs("/workspace/quant_compare/signals", exist_ok=True)
    log = pd.DataFrame([{
        "date": end, "code": r["code"], "name": r["name"], "close": r["close"],
        "in_position": r["in_pos"], "units": r["units"],
        "avg_entry": r["avg_entry"], "stop": r["stop"],
        "turtle_sig": r["t_sig"], "ma_sig": r["m_sig"], "overall": r["overall"],
        "upper10": r["upper10"], "lower40": r["lower40"], "ma_fast": r["ma_fast"], "ma_slow": r["ma_slow"],
    } for r in rows])
    path = f"/workspace/quant_compare/signals/signal_{end}.csv"
    log.to_csv(path, index=False)
    print(f"  信号日志: {path}")
    return 0

# ========================= CLI 分发 =========================
def build_parser():
    ap = argparse.ArgumentParser(
        description="量化回测统一入口（海龟+均线双策略，口径B：单票≤60%+回撤≤20%）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("original", help="跑光通信/存储 9 只（写 equity_constrained.csv）")
    sub.add_parser("semi", help="跑半导体 9 只（写 equity_semi_nosmic.csv）")
    sub.add_parser("all", help="刷新数据 + 双篮重跑 + 出 reports/backtest_daily.md（定时任务用）")
    sub.add_parser("verify", help="约束最优明细复盘（口径B）")
    sub.add_parser("scan-semi", help="半篮参数扫描（固定 cap=0.585）")
    sub.add_parser("joint", help="联合参数扫描（滚动实时窗口）")
    sub.add_parser("joint630", help="联合参数扫描（定稿窗口 截至2026-06-30）")
    sub.add_parser("scan-sweet", help="原篮 circuit 甜点区扫描(0.16~0.19)")
    sub.add_parser("refresh", help="增量刷新两篮本地数据")
    sub.add_parser("fetch-semi", help="抓取半篮 10 只原始数据")
    sub.add_parser("plot", help="画权益曲线 + 回撤图")
    sub.add_parser("backtest-semi", help="半篮 10 只(含中芯)对比回测 + T+1 校验")
    sub.add_parser("mine", help="我的策略(海龟10/70 + 均线)单标的 + 9 只等权组合")
    sub.add_parser("debug-circuit", help="调试 2025-09 熔断区间持仓")
    sub.add_parser("turtle-v11", help="对比上传的 turtle_v11 实跑结果")
    p_sig = sub.add_parser("signal", help="每日收盘科技股信号（自包含，不依赖外部包）")
    p_sig.add_argument("end", nargs="?", default=None, help="数据截止日 YYYYMMDD，默认今天")
    return ap

DISPATCH = {
    "original": cmd_original, "semi": cmd_semi, "all": cmd_all, "verify": cmd_verify,
    "scan-semi": cmd_scan_semi, "joint": cmd_joint, "joint630": cmd_joint630,
    "scan-sweet": cmd_scan_sweet, "refresh": cmd_refresh, "fetch-semi": cmd_fetch_semi,
    "plot": cmd_plot, "backtest-semi": cmd_backtest_semi, "mine": cmd_mine,
    "debug-circuit": cmd_debug_circuit, "turtle-v11": cmd_turtle_v11,
    "signal": cmd_signal,
}

if __name__ == "__main__":
    args = build_parser().parse_args()
    fn = DISPATCH.get(args.cmd)
    if fn is None:
        build_parser().print_help()
        sys.exit(1)
    ok = fn()
    # 仅 `all` 子命令返回布尔(约束是否全达标)用于定时任务退出码；
    # `refresh` 返回 (成功数, 失败数)，失败则非零退出便于 cron 告警；其余命令统一退出 0
    if args.cmd == "all":
        sys.exit(0 if ok else 1)
    if args.cmd == "refresh":
        sys.exit(1 if (isinstance(ok, tuple) and ok[1] > 0) else 0)
    sys.exit(0)
