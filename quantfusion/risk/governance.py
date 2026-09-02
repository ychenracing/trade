"""风险治理层：预热健康契约、风险意见对象、风险事件校准与篮覆盖置信度。

本模块落实《trade 深度评估与长板强化改造报告》(2026-08-16) 的 P0-1/P0-2/
P0-3 与 P1-1/P1-2，全部为纯函数/纯数据结构，默认随引擎自动输出，不改变
任何交易决策路径（P0-4 晋级门在 ``quantfusion.application.stress`` 中实现）。

注：本文件中的 P0/P1 编号均指 2026-08-16 报告；仓库旧注释（2026-08-07
报告）使用另一套编号（如 P0-4=灾变冷却阻断再入场、P1-2=子行业参数收缩），
两套体系互不相干，以报告日期区分：

- P0-1 Warmup Health Contract：``assess_warmup_health`` 输出
  READY/DEGRADED/NOT_READY 三级预热健康报告；
- P0-2 Risk Event Classifier：``calibrate_risk_events`` 事后计算每次风险
  事件的 1/3/5/10/20 日结果与 Precision/Recall/Lead time 等校准指标，
  并附 P1-3 所需的 L1 冻结加仓机会成本度量；
- P0-3 独立风险意见对象：``RiskOpinion`` 与 ``build_risk_opinion`` 生成与
  交易动作分离的标准风险意见；
- P1-1 Sleeve 分歧证据：``compute_sleeve_agreement`` 输出组合级袖套共识
  与持续退化计数（只作证据，不新增状态机）；
- P1-2 Risk Basket Coverage Confidence：``basket_coverage_confidence``
  根据风险篮观察覆盖度计算风险置信度。

所有输出均为 JSON 可序列化的普通 Python 数据（经 ``as_dict``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, cast

import numpy as np
import pandas as pd

# P0-1: 一只股票被认为"指标就绪"所需的最少预热交易日数。最长指标窗口为
# 弱市 240 日动量与 120 日相对强度，240 个交易日覆盖全部指标需求。
REQUIRED_WARMUP_TRADING_DAYS = 240

# P0-1: 外层路由指数 (000300/000682) 判定"新鲜"的最大陈旧自然日数，与
# quantfusion.config.regime.MAX_EVIDENCE_STALENESS_DAYS 保持一致。
WARMUP_STALENESS_DAYS = 10

# P0-1: 分级阈值。指标就绪比例低于该值时整体判为 NOT_READY。
NOT_READY_INDICATOR_RATIO = 0.5

# P0-2: 判定"已实现冲击"(realized shock) 的前瞻窗口与回撤阈值。阈值与
# overlay 的 L2 账户回撤门槛 (RISK_LEVEL2_DRAWDOWN=0.08) 对齐。
SHOCK_HORIZON_DAYS = 20
SHOCK_DRAWDOWN = 0.08

# P0-2: 警报前瞻评估窗口（事件结果表）。
EVENT_OUTCOME_HORIZONS = (1, 3, 5, 10, 20)

# P0-2: 相邻已实现冲击日合并为同一 episode 的最大间隔交易日。
SHOCK_EPISODE_MERGE_DAYS = 5

# P1-2: 风险置信度三因子权重（观察成分比例 / 行业覆盖 / 持仓映射匹配）。
COVERAGE_WEIGHT_OBSERVED = 0.45
COVERAGE_WEIGHT_INDUSTRY = 0.35
COVERAGE_WEIGHT_HELD = 0.20

# P1-2: 风险置信度低于该值时，意见对象标记 low_basket_coverage。
LOW_COVERAGE_CONFIDENCE = 0.60

# P0-3: 各风险等级对应的建议总敞口上限（与 overlay trim 比例互为补数）。
GROSS_CAP_BY_LEVEL = {0: 1.0, 1: 1.0, 2: 0.70, 3: 0.50}

# P1-1: 组合级 sleeve 共识连续下降多少日后在意见中标记风险证据。
SLEEVE_CONSENSUS_DECLINE_DAYS = 3


def _trading_days_before(frame: pd.DataFrame, start_ts: pd.Timestamp) -> int:
    """返回某只股票在回测开始日之前的可用交易日数。"""
    if frame is None or len(frame.index) == 0:
        return 0
    idx = frame.index
    return int((idx < start_ts).sum())


def _natural_days_stale(frame: pd.DataFrame, end_ts: pd.Timestamp) -> int:
    """返回股票最新数据距回测结束日的自然日数（衡量陈旧度）。"""
    if frame is None or len(frame.index) == 0:
        return 10**6
    return int((end_ts - frame.index[-1]).days)


@dataclass(frozen=True)
class WarmupHealthReport:
    """P0-1 预热健康报告：回测/生产运行的数据预热质量契约。

    生产规则：
    - ``NOT_READY``：禁止把输出当成正式交易信号；
    - ``DEGRADED``：风险判断可保留，新增风险动作降级/人工确认；
    - ``READY``：正常使用。
    """

    warmup_status: str
    required_days: int
    indicator_ready_ratio: float
    reference_basket_ready_ratio: float
    regime_index_ready: bool
    sleeve_state_ready: bool
    new_symbol_count: int
    stale_symbol_count: int
    history_days_available: dict[str, int]
    new_symbols: tuple[str, ...] = ()
    stale_symbols: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化的字典表示。"""
        return {
            "warmup_status": self.warmup_status,
            "required_days": int(self.required_days),
            "indicator_ready_ratio": round(self.indicator_ready_ratio, 4),
            "reference_basket_ready_ratio": round(
                self.reference_basket_ready_ratio, 4
            ),
            "regime_index_ready": self.regime_index_ready,
            "sleeve_state_ready": self.sleeve_state_ready,
            "new_symbol_count": int(self.new_symbol_count),
            "stale_symbol_count": int(self.stale_symbol_count),
            "history_days_available": {
                k: int(v) for k, v in self.history_days_available.items()
            },
            "new_symbols": list(self.new_symbols),
            "stale_symbols": list(self.stale_symbols),
            "reasons": list(self.reasons),
        }


def assess_warmup_health(
    data_map: dict[str, pd.DataFrame],
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp,
    *,
    reference_symbols: Sequence[str] = (),
    reference_frames: dict[str, pd.DataFrame] | None = None,
    regime_index_frames: dict[str, pd.DataFrame] | None = None,
    required_days: int = REQUIRED_WARMUP_TRADING_DAYS,
    staleness_days: int = WARMUP_STALENESS_DAYS,
) -> WarmupHealthReport:
    """评估一次运行的预热健康状态（P0-1 Warmup Health Contract）。

    逐股票统计回测开始日之前的可用交易日数；新上市（预热不足）股票不会
    静默获得与成熟股票相同的置信度，而是把整体状态降级为 DEGRADED。
    regime 证据完全缺失或指标就绪比例过低时判为 NOT_READY。

    - ``reference_symbols`` / ``reference_frames``：独立风险篮（23 股）。
      传入 ``reference_frames`` 时就绪度按篮内实际可观察帧计算（缺失成分
      显式计入未就绪），否则退回交易池内的参考成分。
    - ``regime_index_frames``：本次运行实际可用的 regime 证据帧（袖套
      regime 参考篮在场成员，或外层双指数帧）。完全缺失时判 NOT_READY
      （风险层失明，失败关闭）；存在但陈旧/历史不足时仅降级为 DEGRADED
      并输出 ``regime_index_stale`` 原因（数据质量问题显式可见）。
    """
    start_ts = cast(pd.Timestamp, pd.Timestamp(start_date))
    end_ts = cast(pd.Timestamp, pd.Timestamp(end_date))
    symbols = sorted(data_map)
    history = {
        code: _trading_days_before(data_map[code], start_ts) for code in symbols
    }
    ready = [code for code in symbols if history[code] >= required_days]
    new_symbols = tuple(code for code in symbols if history[code] < required_days)
    stale = tuple(
        code
        for code in symbols
        if _natural_days_stale(data_map[code], end_ts) > staleness_days
    )
    ratio = (len(ready) / len(symbols)) if symbols else 1.0

    if reference_frames is not None:
        ref = tuple(reference_symbols) or tuple(reference_frames)
        ref_ready = [
            c
            for c in ref
            if c in reference_frames
            and _trading_days_before(reference_frames[c], start_ts) >= required_days
        ]
    else:
        ref = tuple(reference_symbols) or tuple(symbols)
        ref_ready = [c for c in ref if history.get(c, 0) >= required_days]
    ref_ratio = (len(ref_ready) / len(ref)) if ref else 1.0

    regime_frames = dict(regime_index_frames or {})
    regime_missing = not regime_frames
    regime_stale = bool(regime_frames) and any(
        frame is None
        or len(frame.index) < 120
        or _natural_days_stale(frame, end_ts) > staleness_days
        for frame in regime_frames.values()
    )
    regime_ready = (not regime_missing) and (not regime_stale)

    reasons: list[str] = []
    if regime_missing:
        reasons.append("regime_index_missing_or_stale")
    if regime_stale:
        reasons.append("regime_index_stale")
    if ratio < 1.0:
        reasons.append("indicator_warmup_incomplete")
    if new_symbols:
        reasons.append(f"new_symbols_without_full_history:{len(new_symbols)}")
    if stale:
        reasons.append(f"stale_symbols:{len(stale)}")
    if ref_ratio < 1.0:
        reasons.append("reference_basket_incomplete")

    # 失败关闭层级：regime 证据完全缺失（风险层失明）或指标就绪比例过低
    # 时判 NOT_READY；陈旧/缺参考成分等数据质量问题降级为 DEGRADED。
    if regime_missing or ratio < NOT_READY_INDICATOR_RATIO:
        status = "NOT_READY"
    elif reasons:
        status = "DEGRADED"
    else:
        status = "READY"

    return WarmupHealthReport(
        warmup_status=status,
        required_days=int(required_days),
        indicator_ready_ratio=ratio,
        reference_basket_ready_ratio=ref_ratio,
        regime_index_ready=regime_ready,
        sleeve_state_ready=ratio >= NOT_READY_INDICATOR_RATIO and regime_ready,
        new_symbol_count=len(new_symbols),
        stale_symbol_count=len(stale),
        history_days_available=history,
        new_symbols=new_symbols,
        stale_symbols=stale,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class BasketCoverage:
    """P1-2 风险篮覆盖度：观察成分、行业覆盖与持仓映射匹配。"""

    observed: int
    total_basket: int
    observed_industries: int
    total_industries: int
    held_symbols: tuple[str, ...]
    held_mapped_ratio: float
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化的字典表示。"""
        return {
            "observed": int(self.observed),
            "total_basket": int(self.total_basket),
            "observed_ratio": round(
                self.observed / self.total_basket, 4
            ) if self.total_basket else 0.0,
            "observed_industries": int(self.observed_industries),
            "total_industries": int(self.total_industries),
            "industry_coverage": round(
                self.observed_industries / self.total_industries, 4
            ) if self.total_industries else 0.0,
            "held_symbols": list(self.held_symbols),
            "held_mapped_ratio": round(self.held_mapped_ratio, 4),
            "confidence": round(self.confidence, 4),
        }


def basket_coverage_confidence(
    observed: int,
    total_basket: int,
    observed_industries: int,
    total_industries: int,
    held_symbols: Iterable[str],
    symbol_sub_industry: dict[str, str],
) -> BasketCoverage:
    """根据风险篮覆盖度计算当日风险置信度（P1-2）。

    置信度 = 0.45 × 观察成分比例 + 0.35 × 行业覆盖比例 + 0.20 × 持仓
    子行业映射比例。覆盖不足时置信度下降，上层据此降低风险意见强度而
    不是继续输出同样的 L2/L3 确信度。
    """
    observed_ratio = observed / total_basket if total_basket else 0.0
    industry_ratio = (
        observed_industries / total_industries if total_industries else 0.0
    )
    held = tuple(sorted(set(held_symbols)))
    mapped = sum(1 for s in held if s in symbol_sub_industry)
    held_ratio = (mapped / len(held)) if held else 1.0
    confidence = (
        COVERAGE_WEIGHT_OBSERVED * observed_ratio
        + COVERAGE_WEIGHT_INDUSTRY * industry_ratio
        + COVERAGE_WEIGHT_HELD * held_ratio
    )
    return BasketCoverage(
        observed=int(observed),
        total_basket=int(total_basket),
        observed_industries=int(observed_industries),
        total_industries=int(total_industries),
        held_symbols=held,
        held_mapped_ratio=held_ratio,
        confidence=float(min(max(confidence, 0.0), 1.0)),
    )


@dataclass(frozen=True)
class RiskOpinion:
    """P0-3 独立风险意见对象：与具体买卖订单分离的标准风险判断。

    该对象回答"当前风险环境如何"，供人工决策或其他系统作为独立风险
    裁判意见消费；不直接驱动任何交易动作。
    """

    date: str
    risk_level: int
    risk_confidence: float
    regime: str
    bull_silent: bool
    block_new_entries: bool
    block_pyramids: bool
    recommended_gross_cap: float
    weakest_clusters: tuple[str, ...]
    reason_codes: tuple[str, ...]
    coverage: dict[str, Any] = field(default_factory=dict)
    sleeve_consensus: float | None = None
    sleeve_consensus_decline_streak: int = 0

    def as_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化的字典表示。"""
        return {
            "date": self.date,
            "risk_level": int(self.risk_level),
            "risk_confidence": round(float(self.risk_confidence), 4),
            "regime": self.regime,
            "bull_silent": bool(self.bull_silent),
            "block_new_entries": bool(self.block_new_entries),
            "block_pyramids": bool(self.block_pyramids),
            "recommended_gross_cap": float(self.recommended_gross_cap),
            "weakest_clusters": list(self.weakest_clusters),
            "reason_codes": list(self.reason_codes),
            "coverage": dict(self.coverage),
            "sleeve_consensus": (
                None
                if self.sleeve_consensus is None
                else round(float(self.sleeve_consensus), 4)
            ),
            "sleeve_consensus_decline_streak": int(
                self.sleeve_consensus_decline_streak
            ),
        }


def build_risk_opinion(
    date: str,
    risk_level: int,
    coverage: BasketCoverage,
    *,
    regime: str = "trend",
    stressed_sub_industry: str | None = None,
    catastrophe_cooldown_active: bool = False,
    outer_route: str | None = None,
    sleeve_consensus: float | None = None,
    sleeve_consensus_decline_streak: int = 0,
    blocks_new_entries: bool | None = None,
    blocks_pyramiding: bool | None = None,
) -> RiskOpinion:
    """从 overlay 状态与覆盖度证据构建当日独立风险意见（P0-3）。

    ``blocks_new_entries`` / ``blocks_pyramiding`` 缺省时按 overlay 的分级
    语义推导（level>=2 禁新开仓、level>=1 冻结加仓），与实际执行行为
    保持一致；意见本身只描述环境，不产生订单。
    """
    level = int(max(0, min(3, risk_level)))
    reasons: list[str] = []
    if level == 1:
        reasons.append("sector_warning_armed")
    elif level == 2:
        reasons.append("sector_risk_confirmed")
    elif level >= 3:
        reasons.append("sustained_risk_failure")
    if stressed_sub_industry:
        reasons.append(f"subindustry_stress:{stressed_sub_industry}")
    if coverage.confidence < LOW_COVERAGE_CONFIDENCE:
        reasons.append("low_basket_coverage")
    if catastrophe_cooldown_active:
        reasons.append("catastrophe_cooldown_active")
    if outer_route in {"weak", "cash", "transition_to_trend"}:
        reasons.append(f"outer_route_defensive:{outer_route}")
    if (
        sleeve_consensus_decline_streak >= SLEEVE_CONSENSUS_DECLINE_DAYS
    ):
        reasons.append("sleeve_consensus_declining")
    if coverage.held_symbols and coverage.held_mapped_ratio < 1.0:
        reasons.append("held_book_unmapped")

    block_new = (level >= 2) if blocks_new_entries is None else blocks_new_entries
    block_pyr = (level >= 1) if blocks_pyramiding is None else blocks_pyramiding

    return RiskOpinion(
        date=date,
        risk_level=level,
        risk_confidence=coverage.confidence,
        regime=str(regime),
        bull_silent=(level == 0 and not catastrophe_cooldown_active),
        block_new_entries=block_new,
        block_pyramids=block_pyr,
        recommended_gross_cap=float(GROSS_CAP_BY_LEVEL.get(level, 1.0)),
        weakest_clusters=(
            (stressed_sub_industry,) if stressed_sub_industry else ()
        ),
        reason_codes=tuple(reasons),
        coverage=coverage.as_dict(),
        sleeve_consensus=sleeve_consensus,
        sleeve_consensus_decline_streak=int(sleeve_consensus_decline_streak),
    )


# ── P0-2: Risk Event Classifier ──────────────────────────────────────────


def _forward_max_drawdown(assets: np.ndarray) -> np.ndarray:
    """对每个日期计算未来 SHOCK_HORIZON_DAYS 日内的最大跌幅。"""
    n = len(assets)
    fwd = np.zeros(n, dtype=float)
    for i in range(n):
        window = assets[i: i + SHOCK_HORIZON_DAYS + 1]
        if len(window) >= 2:
            fwd[i] = float(window.min() / window[0] - 1.0)
    return fwd


def _episodes(flags: np.ndarray, merge_gap: int) -> list[tuple[int, int]]:
    """把布尔序列合并为 (start, end) 闭区间 episode 列表。"""
    episodes: list[tuple[int, int]] = []
    start = None
    last_true = -10**9
    for i, flag in enumerate(flags):
        if flag:
            if start is None or i - last_true > merge_gap + 1:
                if start is not None:
                    episodes.append((start, last_true))
                start = i
            last_true = i
    if start is not None:
        episodes.append((start, last_true))
    return episodes


def _window_stats(
    assets: np.ndarray,
    basket_returns: np.ndarray | None,
    pos: int,
    horizon: int,
) -> dict[str, Any]:
    """事件后 horizon 日的组合/风险篮最低收益与最大回撤、是否恢复。"""
    end = min(pos + horizon, len(assets) - 1)
    if end <= pos:
        return {
            "portfolio_min_return": 0.0,
            "basket_min_return": None,
            "max_drawdown": 0.0,
            "recovered": True,
        }
    window = assets[pos: end + 1]
    min_ret = float(window.min() / assets[pos] - 1.0)
    peak = np.maximum.accumulate(window)
    dd = float((window / peak - 1.0).min())
    basket_min = None
    if basket_returns is not None and end > pos:
        bw = basket_returns[pos + 1: end + 1]
        if len(bw):
            basket_min = float(np.cumprod(1.0 + bw).min() - 1.0)
    recovered = bool(assets[end] >= assets[pos])
    return {
        "portfolio_min_return": round(min_ret, 6),
        "basket_min_return": None if basket_min is None else round(basket_min, 6),
        "max_drawdown": round(dd, 6),
        "recovered": recovered,
    }


def calibrate_risk_events(
    dates: Sequence[str],
    assets: Sequence[float],
    risk_levels: Sequence[int],
    *,
    basket_daily_returns: Sequence[float] | None = None,
    shock_drawdown: float = SHOCK_DRAWDOWN,
) -> dict[str, Any]:
    """事后校准风险事件分类器（P0-2 Risk Event Classifier）。

    输入逐日资产与逐日风险等级序列，独立检测"已实现冲击"（未来 20 日
    组合回撤超过阈值），然后对每次 L1/L2/L3 警报计算 1/3/5/10/20 日的
    组合最低收益、风险篮最低收益、最大回撤与恢复状态，并汇总：
    Shock Precision / Shock Recall / Median Lead Time / False Positive
    Cost / Missed Crash Loss / Bull Silence Ratio / L1→L2 escalation
    precision。
    """
    n = len(assets)
    if n == 0 or len(risk_levels) != n:
        return {"status": "insufficient_data", "events": [], "metrics": {}}
    arr = np.asarray(assets, dtype=float)
    levels = np.asarray(risk_levels, dtype=int)
    fwd_dd = _forward_max_drawdown(arr)
    shock_flags = fwd_dd <= -abs(shock_drawdown)
    shock_eps = _episodes(shock_flags, SHOCK_EPISODE_MERGE_DAYS)

    basket = (
        np.asarray(basket_daily_returns, dtype=float)
        if basket_daily_returns is not None and len(basket_daily_returns) == n
        else None
    )

    # 警报 episode：risk level 从 0 升到 >=1 的连续段。
    alert_eps = _episodes(levels >= 1, 0)

    events: list[dict[str, Any]] = []
    tp = 0
    fp_followup_returns: list[float] = []
    l1_post_freeze_returns: list[float] = []
    for start, end in alert_eps:
        level_peak = int(levels[start: end + 1].max())
        # TP 判定：警报区间与已实现冲击区间重叠，或冲击在警报结束后
        # 5 日（episode 合并间隔）内开始。
        realized = any(
            s <= end + SHOCK_EPISODE_MERGE_DAYS and e >= start
            for s, e in shock_eps
        )
        if realized:
            tp += 1
        else:
            horizon_end = min(start + SHOCK_HORIZON_DAYS, n - 1)
            if horizon_end > start:
                fp_followup_returns.append(
                    float(arr[horizon_end] / arr[start] - 1.0)
                )
        # P1-3: 峰值停留在 L1（冻结加仓但未升级 L2/L3）的警报段，
        # 记录警报结束后 20 日组合收益 —— 若普遍为正，说明冻结加仓
        # 多数落在牛市正常回踩上，存在机会成本。
        if level_peak == 1 and end + 1 < n:
            follow_end = min(end + SHOCK_HORIZON_DAYS, n - 1)
            l1_post_freeze_returns.append(
                float(arr[follow_end] / arr[end] - 1.0)
            )
        events.append(
            {
                "alert_date": dates[start],
                "alert_end_date": dates[end],
                "peak_level": level_peak,
                "duration_days": int(end - start + 1),
                "realized_shock": bool(realized),
                "outcomes": {
                    f"{h}d": _window_stats(arr, basket, start, h)
                    for h in EVENT_OUTCOME_HORIZONS
                },
            }
        )

    # Recall 与 lead time：冲击 episode (s, e) 是"前视窗口能捕获到 ≥8%
    # 回撤"的日期段，冲击在 e+1（兑现日 m）附近落地。判定某警报成功检测
    # 需满足两点：警报开始不晚于兑现日（as_ <= m），且警报未在警告窗
    # 开始前过早结束（ae >= s - 合并间隔）。lead time = m - 最早警报
    # 开始日，即警报领先冲击兑现的交易日数。
    detected = 0
    lead_times: list[int] = []
    missed_depths: list[float] = []
    for s, e in shock_eps:
        m = e + 1  # 冲击兑现日（episode 结束次日）
        overlapping = [
            (as_, ae)
            for as_, ae in alert_eps
            if as_ <= m and ae >= s - SHOCK_EPISODE_MERGE_DAYS
        ]
        if overlapping:
            detected += 1
            first_alert_start = min(as_ for as_, _ in overlapping)
            lead_times.append(int(max(0, m - first_alert_start)))
        else:
            missed_depths.append(float(fwd_dd[s: e + 1].min()))

    alert_days = int((levels >= 1).sum())
    # L1→L2 escalation precision：峰值停留在 L1 的警报段中，随后 20 日
    # 内升级到 >=L2 的比例。
    l1_total = 0
    escalated = 0
    for start, end in alert_eps:
        if int(levels[start: end + 1].max()) == 1:
            l1_total += 1
            future = levels[end + 1: end + 1 + SHOCK_HORIZON_DAYS]
            if len(future) and int(future.max()) >= 2:
                escalated += 1

    metrics = {
        "alert_episode_count": len(alert_eps),
        "shock_episode_count": len(shock_eps),
        "shock_precision": round(tp / len(alert_eps), 4) if alert_eps else None,
        "shock_recall": (
            round(detected / len(shock_eps), 4) if shock_eps else None
        ),
        "median_lead_time_days": (
            int(np.median(lead_times)) if lead_times else None
        ),
        "false_positive_count": len(alert_eps) - tp,
        "false_positive_cost_median": (
            round(float(np.median(fp_followup_returns)), 4)
            if fp_followup_returns
            else None
        ),
        "missed_crash_count": len(shock_eps) - detected,
        "missed_crash_loss_median": (
            round(float(np.median(missed_depths)), 4) if missed_depths else None
        ),
        "bull_silence_ratio": (
            round(1.0 - alert_days / n, 4) if n else None
        ),
        "l1_escalation_precision": (
            round(escalated / l1_total, 4) if l1_total else None
        ),
        # P1-3: L1 冻结加仓的机会成本度量（警报结束后 20 日收益中位数，
        # 正值越大说明越多 L1 只是牛市正常回踩）。
        "l1_only_episode_count": l1_total,
        "l1_only_median_post_return_20d": (
            round(float(np.median(l1_post_freeze_returns)), 4)
            if l1_post_freeze_returns
            else None
        ),
    }
    return {
        "status": "ok",
        "shock_drawdown_threshold": float(shock_drawdown),
        "shock_horizon_days": int(SHOCK_HORIZON_DAYS),
        "events": events,
        "metrics": metrics,
    }


# ── P1-1: Sleeve agreement evidence ──────────────────────────────────────


@dataclass(frozen=True)
class SleeveAgreementSnapshot:
    """P1-1 三袖套分歧证据：某日的跨袖套共识度量。

    纯观测证据，不驱动交易。``mean_consensus`` 是每只被持有股票的平均
    持有袖套数（0~1 归一），``decline_streak`` 度量 3→2→1 的持续退化，
    ``weakest_sleeve`` 指出部署率最低（最先失效）的袖套。
    """

    date: str
    mean_consensus: float
    symbols_by_three: int
    symbols_by_two: int
    symbols_by_one: int
    sleeve_deployment: dict[str, float]
    weakest_sleeve: str | None
    decline_streak: int

    def as_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化的字典表示。"""
        return {
            "date": self.date,
            "mean_consensus": round(self.mean_consensus, 4),
            "symbols_by_three": int(self.symbols_by_three),
            "symbols_by_two": int(self.symbols_by_two),
            "symbols_by_one": int(self.symbols_by_one),
            "sleeve_deployment": {
                k: round(v, 4) for k, v in self.sleeve_deployment.items()
            },
            "weakest_sleeve": self.weakest_sleeve,
            "decline_streak": int(self.decline_streak),
        }


def compute_sleeve_agreement(
    date: str,
    sleeve_names: Sequence[str],
    held_symbols_per_sleeve: Sequence[set[str]],
    sleeve_assets: Sequence[float],
    sleeve_cash: Sequence[float],
    *,
    previous_consensus: float | None = None,
    previous_streak: int = 0,
) -> SleeveAgreementSnapshot:
    """计算某日三袖套共识快照（P1-1 sleeve disagreement evidence）。

    输入每个袖套当日持有的股票集合与资产/现金，输出组合级共识指标；
    与前一日共识比较得到连续退化天数（3→2→1 的持续时间代理）。
    """
    all_held: set[str] = set()
    for held in held_symbols_per_sleeve:
        all_held |= set(held)
    counts = {
        sym: sum(1 for held in held_symbols_per_sleeve if sym in held)
        for sym in all_held
    }
    n_sleeves = max(len(sleeve_names), 1)
    mean_consensus = (
        sum(counts.values()) / (len(counts) * n_sleeves) if counts else 1.0
    )
    by_three = sum(1 for c in counts.values() if c >= 3)
    by_two = sum(1 for c in counts.values() if c == 2)
    by_one = sum(1 for c in counts.values() if c == 1)

    deployment: dict[str, float] = {}
    for name, assets, cash in zip(sleeve_names, sleeve_assets, sleeve_cash):
        total = float(assets)
        deployed = max(total - float(cash), 0.0)
        deployment[str(name)] = deployed / total if total > 0 else 0.0
    # 空仓簿（三袖套全为现金）不存在"最弱袖套"与共识退化证据：
    # 共识按惯例记满值、weakest_sleeve 置空、退化计数清零。
    weakest = (
        min(deployment, key=lambda name: deployment[name])
        if deployment and all_held
        else None
    )

    if not all_held:
        streak = 0
    elif previous_consensus is not None and mean_consensus < previous_consensus:
        streak = previous_streak + 1
    else:
        streak = 0

    return SleeveAgreementSnapshot(
        date=date,
        mean_consensus=float(mean_consensus),
        symbols_by_three=by_three,
        symbols_by_two=by_two,
        symbols_by_one=by_one,
        sleeve_deployment=deployment,
        weakest_sleeve=weakest,
        decline_streak=int(streak),
    )
