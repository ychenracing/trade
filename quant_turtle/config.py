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
    cache_dir: str = "/workspace/quant_turtle/cache"

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
