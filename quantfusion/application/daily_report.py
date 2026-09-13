"""Read-only, source-bound explanations of the existing daily decisions."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Labels describe recorded facts; they never calculate signals or risk gates.
_STRATEGIES = {
    "turtle_breakout": "突破与顺势加仓策略",
    "dual_ma": "短期与长期均价趋势策略",
    "atr_channel": "价格波动通道策略",
    "positive_momentum_hold": "弱市相对强势股筛选",
    "account_position": "真实持仓检查",
}
_ROUTES = {
    "frozen_trend_engine": "跟随已经形成的上涨趋势",
    "positive_momentum_hold": "市场偏弱，只观察筛选出的相对强势股",
    "cash_preservation": "以保留现金为主",
}
_REASONS = {
    "account_budget_trim": "账户整体风险预算要求减少持仓；这是计划，不代表已成交",
    "account_budget_buy_reduced": "买入数量被风险预算缩减；未成交的卖出不增加可用买入资金",
    "ACCOUNT_RISK_BUDGET": "账户整体风险预算限制新增买入",
    "Donchian breakout": "价格突破前期高点，趋势强度条件已满足",
    "Turtle breakout": "价格突破前期高点，趋势强度条件已满足",
    "Turtle pyramid add": "价格继续上涨，策略发出顺势加仓提示",
    "MA golden cross": "短期均价向上穿过长期均价，并满足策略的强弱条件",
    "ATR channel breakout": "价格突破按近期波动计算的上方通道，并满足趋势强度条件",
    "ATR trailing stop": "价格触及随行情上移的保护价",
    "ATR lower-channel exit": "价格跌破下方波动通道",
    "Donchian exit": "价格跌到策略观察期的低位退出线",
    "hard stop": "价格触及该策略的止损条件",
    "reversal profit protection": "前期盈利回吐达到保护条件",
    "short-term reversal breakdown": "盈利回吐、短期低点和短期均价共同显示转弱",
    "failed-trend reversal exit": "持仓亏损且短期均价继续走弱，趋势失败退出",
    "short trend is below the long trend": "短期均价低于长期均价，且价格低于短期均价",
    "current route is cash preservation": "当前市场判断要求以保留现金为主",
    "holding is outside the current weak-regime leaders": "持仓不在本次弱市相对强势股名单内",
    "no account-specific exit condition": "本次未触发真实持仓退出条件；不代表没有风险",
    "positive_momentum_hold selection": "已入选本次弱市相对强势股名单",
    "PEAK_EVIDENCE_INCOMPLETE": "持仓最高价记录不完整，不能把局部最高价当作完整历史峰值",
    "MARKET_DATA_UNAVAILABLE": "所需股票行情不可用",
    "LEADER_DATA_UNAVAILABLE": "强势股筛选所需行情不可用",
    "LEADER_EVIDENCE_UNAVAILABLE": "强势股筛选依据未提供",
    "LEADER_EVIDENCE_INCOMPLETE": "强势股筛选依据不完整",
    "LEADER_REQUESTED_SYMBOLS_MISMATCH": "请求股票与筛选依据中的股票不一致",
    "INCONSISTENT_EVIDENCE_DATE": "股票行情的实际截止日期不一致",
    "TRADING_DAY_COVERAGE": "缺少应覆盖交易日的日线，或无法核验停牌；不能按节假日处理",
    "INDEX_EVIDENCE_UNAVAILABLE": "固定指数证据缺失、无效或未覆盖目标交易日",
    "CALENDAR_OUT_OF_RANGE": "请求超出已核验交易日历范围，不能推定开市日期",
    "CALENDAR_UNAVAILABLE": "交易日历不可用或来源范围无法核验",
    "FUTURE_EVIDENCE": "行情观测晚于请求截止日期，不能用于本次决策",
    "CLOSE_NOT_READY": "上海时区当日完整收盘数据尚未就绪",
    "PROVIDER_STALE_OR_AGE": "数据源标记陈旧或已超过原有自然日容忍范围",
    "required account evidence is incomplete": "真实账户判断所需数据不完整",
    "actual market evidence dates are inconsistent": "股票行情的实际截止日期不一致",
    "provider-marked stale": "数据源已标记行情陈旧",
    "no market data": "没有可用行情",
    "sector_warning_armed": "板块出现风险预警",
    "sector_risk_confirmed": "板块风险已得到进一步确认",
    "sustained_risk_failure": "风险持续恶化",
    "subindustry_stress": "部分细分行业承压",
    "low_basket_coverage": "风险观察股票的覆盖不足，判断依据较弱",
    "catastrophe_cooldown_active": "严重风险后的冷却限制仍在生效",
    "outer_route_defensive": "市场判断处于防御模式",
    "sleeve_consensus_declining": "多个独立策略账户的持仓一致性持续下降",
    "held_book_unmapped": "部分持仓缺少行业映射",
    "regime_index_missing_or_stale": "市场判断所需指数缺失或不可用",
    "regime_index_stale": "市场判断所需指数陈旧",
    "indicator_warmup_incomplete": "计算指标所需的历史数据不足",
    "new_symbols_without_full_history": "部分股票的上市历史不足",
    "stale_symbols": "部分股票行情陈旧",
    "reference_basket_incomplete": "参考股票数据不完整",
    "account_budget_envelope": "账户整体风险预算已进行评估；评估不等于成交",
    "sector_guard_on": "板块风险限制已开启",
    "sector_guard_off": "板块风险限制已解除",
    "sector_shock": "检测到板块冲击",
    "sector_guard_data_insufficient": "板块风险数据不足",
    "rejected_insufficient_cash": "现金不足，买入被拒绝",
    "rejected_max_positions": "持股数量已达到上限",
    "rejected_position_limit": "持仓额度不足，买入被拒绝",
    "rejected_daily_loss_limit": "当日损失限制阻止买入",
    "rejected_no_exposure_capacity": "可用持仓额度不足，买入被拒绝",
    "rejected_no_shared_batch_capacity": "同批买入没有剩余可分配额度",
    "rejected_no_prior_adv_capacity": "成交量容量不足，买入被拒绝",
    "rejected_limit_up_open": "开盘涨停条件阻止买入",
    "expired_pending_buy": "买入信号已过有效期",
    "rejected_by_execution_checks": "买入未通过执行检查；未提供更具体的原因",
    "deferred_sell_no_prior_adv_capacity": "成交量容量不足，卖出被延后",
    "clipped_to_adv_capacity": "数量受到成交量容量限制",
    "clipped_to_exposure_capacity": "数量受到持仓额度限制",
    "scaled_for_fair_batch_allocation": "同批多个信号按可用额度缩减数量",
    "released_unexecutable_sublot_sell": "无法执行的零散卖出已从本批释放，不能当作成交",
}


def _text(value: Any) -> str:
    if value is None or value == "":
        return "未提供"
    text = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]#|])", r"\\\1", text)


def _number(value: Any, *, percent: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "未提供"
    return f"{value:.2%}" if percent else f"{value:,.2f}".removesuffix(".00")


def _explain(value: Any) -> str:
    if not value:
        return "本次未提供具体原因"
    raw = str(value)
    stop = re.search(r"close ([\d.]+) <= protective stop ([\d.]+)", raw)
    explanations = ([f"收盘价 {stop[1]} 已达到或低于保护价 {stop[2]}"] if stop else [])
    if re.search(r"MA\d+crossed below MA\d+", raw):
        explanations.append("短期均价已低于长期均价，趋势转弱")
    explanations.extend(label for token, label in _REASONS.items() if token in raw)
    if explanations:
        return "；".join(dict.fromkeys(explanations))
    if raw in _STRATEGIES:
        return _STRATEGIES[raw] + "发出提示"
    if re.search(r"[\u4e00-\u9fff]", raw):
        return _text(raw)
    return "存在未翻译的原始依据，请核对文末记录；不能自行推断原因"


def _trace(lines: list[str], label: str, value: Any) -> None:
    if value:
        lines.extend(["", f"### {_text(label)}", "", "<pre>" + html.escape(str(value)) + "</pre>"])


def _suppression(data: dict[str, Any], account: bool) -> list[str]:
    if account:
        reasons = [_explain(r) for r in data.get("buy_suppression_reasons", [])]
        if data.get("data_complete") is False:
            reasons.append("所需行情不完整或实际日期不一致")
        if data.get("valuation_complete") is False:
            reasons.append("持仓估值不完整，不能可靠计算买入额度")
        suppressed = data.get("buys_suppressed")
    else:
        summary = data.get("summary", {})
        reasons = [description for key, description in (
            ("risk_state_identity_mismatch", "股票池或配置与上次不一致，不能确认风险状态连续性"),
            ("current_route_mismatch", "当前市场判断与历史回放结果不一致"),
            ("warmup_not_ready", "计算信号所需的历史数据不足或指数依据不可用"),
        ) if summary.get(key) is True]
        suppressed = summary.get("buys_suppressed")
    if suppressed is True and not reasons:
        reasons.append("本次已阻止买入，但未提供可确认的具体原因")
    return list(dict.fromkeys(reasons))


def _overview(data: dict[str, Any], account: bool, traces: list[str]) -> list[str]:
    day = data.get("as_of" if account else "scan_date")
    decision = data.get("deployment_decision", {}) if account else data.get("deployment", {}).get("current_decision", {})
    lines = ["# 收盘后决策报告", "", f"日期：{_text(day)}｜{'真实账户建议' if account else '模拟信号，不是真实持仓'}", "",
             "仅供人工复核。信号不代表已成交；最早在下一可交易日复核价格、现金、可卖股数和交易限制。",
             "", "## 先看结论", "",
             f"市场判断：{_ROUTES.get(str(decision.get('name', '')), '未提供可识别的市场判断，请核对原始记录')}。"]
    dates = data.get("scan_dates")
    if isinstance(dates, dict) and dates:
        market_dates = ({code: entry.get("evidence_date") for code, entry in data.get("market_evidence", {}).items()}
                        if account else data.get("actual_evidence_dates", {}))
        indices = data.get("index_evidence", {}) if account else data.get("deployment", {}).get("index_evidence", {})
        observed = "、".join(sorted({_text(value) for value in market_dates.values()})) or "未提供"
        index_dates = "、".join(sorted({_text(entry.get("evidence_date")) for entry in indices.values()})) or "未提供"
        lines.extend([
            f"请求截止日期：{_text(dates.get('requested_as_of'))}；应覆盖交易日：{_text(dates.get('required_evidence_date'))}。",
            f"实际股票行情日期：{observed}；实际指数日期：{index_dates}。",
            f"下一可交易日：{_text(dates.get('next_trading_date'))}；市场时区：{_text(dates.get('market_timezone'))}。",
            f"已记录日历覆盖范围：{_text(dates.get('calendar_coverage_start'))} 至 {_text(dates.get('calendar_coverage_end'))}；范围之外不能推定开市。",
        ])
        _trace(traces, "交易日与证据日期", dates)
    else:
        lines.append("未记录交易日覆盖校验，不能从旧报告推定已通过；本页不重新计算或补认证日期。")
    if decision.get("reason"):
        lines.append(f"判断依据：{_explain(decision['reason'])}。")
        _trace(traces, "市场判断", decision)
    if account:
        lines.append(f"账户快照日：{_text(data.get('snapshot_date'))}；行情实际截止日：{_text(data.get('evidence_date'))}。")
        lines.append(f"总资产：{_number(data.get('estimated_equity'))} 元；现金：{_number(data.get('cash'))} 元。")
    else:
        lines.append(f"本次运行标识：{_text(data.get('run_id'))}；市场判断参考边界：{_text(decision.get('boundary'))}。")
        health = data.get("warmup_health") or {}
        label = {"READY": "所需历史数据已就绪", "DEGRADED": "数据有缺口，须谨慎核对",
                 "NOT_READY": "历史数据不足或必要依据不可用，本次买入已被阻止"}.get(str(health.get("warmup_status", "")), "数据就绪情况未提供")
        lines.append(f"数据情况：{label}。")
        for reason in health.get("reasons", []):
            lines.append(f"数据提示：{_explain(reason)}。")
            _trace(traces, "数据提示", reason)
    reasons = _suppression(data, account)
    suppressed = data.get("buys_suppressed") if account else data.get("summary", {}).get("buys_suppressed")
    lines.append("新增买入：本次已统一阻止；卖出风险提示仍需单独复核。" if suppressed is True
                 else "新增买入：未见统一抑制标记不等于买入获准；逐股结论及下一交易日限制仍须复核。")
    lines.extend(f"阻止原因：{r}。" for r in reasons)
    for reason in data.get("buy_suppression_reasons", []):
        _trace(traces, "账户买入限制", reason)
    budget = data.get("account_risk_budget") or {}
    latest = budget.get("latest") or budget
    budget_label = {"APPLIED": "已实际评估（不代表成交或回撤保证）", "NOT_READY": "数据不足，不能据此新增风险",
                    "NOT_EVALUATED": "未完成评估", "DISABLED_DIAGNOSTIC": "已关闭，仅作诊断"}.get(str(budget.get("status", "")), "未提供有效评估状态")
    lines.extend(["", "## 风险与限制", "", f"账户整体风险预算：{budget_label}。"])
    if latest.get("date"):
        lines.append(f"预算观察日：{_text(latest['date'])}。")
    if "buy_scale" in latest:
        lines.append(f"预算计划保留原买入量的比例：{_number(latest['buy_scale'], percent=True)}；仅用于已有计划裁剪，不是建议仓位。")
    if "gross_cap" in latest:
        lines.append(f"按模型压力假设计算的持仓金额上限：{_number(latest['gross_cap'])} 元；不是未来最大亏损保证。")
    opinion = data.get("risk_opinion")
    if not account and isinstance(opinion, dict):
        raw_level = opinion.get("risk_level")
        level_key = raw_level if isinstance(raw_level, int) and not isinstance(raw_level, bool) else -1
        level = {0: "未报告升级警报", 1: "预警，留意新增风险", 2: "风险已确认，需要降低风险", 3: "风险较严重，需要进一步防御"}.get(level_key, "未提供可识别等级")
        lines.append(f"独立风险意见（观察日 {_text(opinion.get('date'))}）：{level}；意见不等于实际拦截，也不改变下面的原始决策。")
        lines.append(f"判断依据覆盖参考值：{_number(opinion.get('risk_confidence'))}，不是涨跌概率。")
        if opinion.get("block_new_entries") is True:
            lines.append("独立意见提示：避免新开仓；请与逐股实际计划分别核对。")
        if opinion.get("block_pyramids") is True:
            lines.append("独立意见提示：暂停对已有持仓继续加仓。")
        for reason in opinion.get("reason_codes", []):
            lines.append(f"识别到的风险：{_explain(reason)}。")
            _trace(traces, "独立风险依据", reason)
        if opinion.get("weakest_clusters"):
            lines.append("承压行业：" + "、".join(_text(x) for x in opinion["weakest_clusters"]) + "。")
    elif account:
        lines.append("真实账户模式不提供模拟账户的独立风险等级；以下依据来自真实持仓、市场判断和账户整体预算，不据此虚构风险评分。")
    else:
        lines.append("独立风险意见未提供；不能把缺少报告理解为没有风险。")
    if not account:
        portfolio = data.get("portfolio", {})
        if portfolio.get("terminal_risk_lock") is True:
            lines.append("模拟账户的终态风险锁仍在生效；不要把报告生成成功当作允许新增风险。")
        if portfolio.get("sector_guard_active") is True:
            lines.append("模拟账户的板块风险限制仍在生效。")
        previous = data.get("previous_risk_state") or {}
        if previous.get("terminal_risk_lock") is True and portfolio.get("terminal_risk_lock") is not True:
            lines.append("上次风险锁已生效，但本次回放未复现；不能据此认定真实账户已解除限制。")
        if previous.get("sector_guard_active") is True and portfolio.get("sector_guard_active") is not True:
            lines.append("上次板块限制已生效，本次回放未复现；需核对变化原因。")
        if data.get("risk_state_saved") is not True:
            lines.append("本次未确认连续风险状态已保存；股票池或配置不匹配时会保留原状态，不应删除状态来强行恢复买入。")
    return lines


def _account_rows(data: dict[str, Any], names: dict[str, str], traces: list[str],
                  overview: list[tuple[str, str, str]]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for row in data.get("actions", []):
        code = row.get("symbol", "")
        seen.add(code)
        action = row.get("action")
        final = {"HOLD": "继续观察现有持仓", "SELL": "出现卖出建议，下一可交易日复核",
                 "REDUCE_REVIEW": "需要人工复核减仓", "DATA_ERROR": "数据不足，无法判断",
                 "BLOCKED": "暂不买入", "BUY_CANDIDATE": "买入候选，尚需人工复核"}.get(action, "未知结论，不能据此交易")
        quantity = row.get("indicative_target_shares")
        if action == "BUY_CANDIDATE" and (not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0):
            final = "候选数量未提供或无效，不能据此买入"
        if action == "BUY_CANDIDATE" and quantity == 0:
            final = "候选数量为零，不能据此买入"
        if action == "BUY_CANDIDATE" and data.get("buys_suppressed") is True:
            final = "本次统一阻止买入，候选不能据此买入"
        name = row.get("name") or names.get(code) or code
        overview.append((_text(name), _text(code), final))
        lines.extend(["", f"### {_text(name)}（{_text(code)}）：{final}", ""])
        strategies = row.get("strategies") or []
        supports = "、".join(_STRATEGIES.get(x, "未翻译的策略，见原始依据") for x in strategies)
        if not supports and "positive_momentum_hold selection" in str(row.get("reason", "")):
            supports = "已入选本次弱市相对强势股名单"
        lines.append(f"支持买入：{supports or '本次未提供已触发的买入依据'}。")
        lines.append("支持卖出／减仓：" + (_explain(row.get("reason")) if action in {"SELL", "REDUCE_REVIEW"}
                    else "本次未提供已触发的卖出依据；缺少依据不等于安全") + "。")
        if action not in {"SELL", "REDUCE_REVIEW"}:
            lines.append(f"当前处理依据：{_explain(row.get('reason'))}。")
        if row.get("shares", 0) > 0:
            lines.append(f"实际持有 {_number(row.get('shares'))} 股；快照可卖 {_number(row.get('sellable_shares'))} 股。")
            lines.append(f"建议卖出／减仓 {_number(row.get('recommended_shares'))} 股；受限制 {_number(row.get('blocked_shares'))} 股；不代表已成交。")
        if row.get("execution_status") in {"T1_BLOCKED", "PARTIALLY_T1_BLOCKED"}:
            lines.append("卖出限制：受买入后次日才能卖的规则影响，不能把全部持仓当作当前可卖；下一可交易日重新核对。")
        elif row.get("execution_status") == "SELLABLE_UNKNOWN":
            lines.append("卖出限制：可卖股数未知，不能据此确定卖出数量；先核对实际账户。")
        elif action in {"SELL", "REDUCE_REVIEW"}:
            lines.append("卖出限制：快照可卖数量只是前提，下一开盘仍可能受停牌、跌停和成交量限制。")
        if "indicative_target_shares" in row:
            lines.append(f"收盘价估算的买入数量：{_number(row.get('indicative_target_shares'))} 股（不是可直接下单的数量）。")
            if "original_indicative_target_shares" in row:
                lines.append(f"预算裁剪前估算数量：{_number(row['original_indicative_target_shares'])} 股。")
        if "close" in row:
            lines.append(f"行情日 {_text(row.get('evidence_date'))}；收盘参考价 {_number(row.get('close'))} 元；保护参考价 {_number(row.get('protective_stop'))} 元，不保证按此价成交。")
        if row.get("peak_evidence_status") == "PEAK_EVIDENCE_INCOMPLETE":
            lines.append("风险信息缺口：持仓最高价记录不完整，部分基于历史峰值的保护无法可靠评估。")
        if action in {"BLOCKED", "BUY_CANDIDATE"}:
            reasons = _suppression(data, True)
            lines.append("买入限制：" + ("；".join(reasons) if reasons else "以本行数量与预算裁剪结果为准；下一开盘仍须核对现金、价格及交易限制") + "。")
        _trace(traces, code, row)
    for code, name in names.items():
        if code not in seen:
            overview.append((_text(name), _text(code), "本次未输出个股建议"))
            lines.extend(["", f"### {_text(name)}（{_text(code)}）：本次未输出个股建议", "",
                          "没有逐股原因记录，不能判断是没有触发信号、没有参与筛选，还是名额等其他条件限制；不可自行补充买卖理由。"])
    return lines


def _simulation_rows(data: dict[str, Any], replay: dict[str, Any], traces: list[str],
                     overview: list[tuple[str, str, str]]) -> list[str]:
    lines: list[str] = []
    pending = data.get("pending_signals", [])
    blocked = data.get("blocked_signals", [])
    reasons = _suppression(data, False)
    suppressed = data.get("summary", {}).get("buys_suppressed") is True
    today = str(data.get("scan_date", ""))
    for row in data.get("signals", []):
        code = row.get("code")
        buys = [s for s in pending if s.get("symbol") == code and s.get("direction") == "buy"]
        sells = [s for s in pending if s.get("symbol") == code and s.get("direction") == "sell"]
        stopped = [s for s in blocked if s.get("symbol") == code]
        valid_buys = [s for s in buys if isinstance(s.get("target_shares"), int)
                      and not isinstance(s["target_shares"], bool) and s["target_shares"] > 0
                      and s.get("executable") is True and s.get("blocked") is False]
        if buys and sells:
            final = "买卖信号并存，分别复核，不相互抵消"
        elif sells:
            final = "存在卖出计划，下一可交易日复核"
        elif valid_buys and not suppressed:
            final = "买入候选，下一可交易日复核"
        elif stopped or (buys and suppressed):
            final = "暂不买入，原买入意向被阻止"
        elif buys:
            final = "买入记录数量或执行标记无效，不能据此买入"
        elif row.get("signal") == "不可交易":
            final = "数据不足，无法判断"
        else:
            final = "现有模拟持仓继续观察" if row.get("held_shares", 0) > 0 else "观望，未保留新买卖计划"
        overview.append((_text(row.get("name")), _text(code), final))
        lines.extend(["", f"### {_text(row.get('name'))}（{_text(code)}）：{final}", "",
                      f"模拟持有 {_number(row.get('held_shares'))} 股；这不是真实账户持仓。"])
        for label, signals in (("支持买入", buys + stopped), ("支持卖出", sells)):
            if not signals:
                lines.append(f"{label}：本次未保留该方向信号；不能据此推断所有规则都未触发。")
            for index, sig in enumerate(signals, 1):
                strategy = _STRATEGIES.get(sig.get("strategy_name"), "未翻译的策略，见原始依据")
                lines.append(f"{label}（记录 {index}）：{strategy}；{_explain(sig.get('reason'))}；计划 {_number(sig.get('target_shares'))} 股，产生于 {_text(sig.get('signal_date'))}。")
                if label == "支持买入" and sig in buys and sig not in valid_buys:
                    lines.append("该买入记录数量或执行标记无效，不能据此买入。")
                if str(sig.get("signal_date", ""))[:10] != today:
                    lines.append("此计划并非本扫描日产生，不要把旧挂单当作今天的新信号。")
                _trace(traces, f"{code} {label} {index}", sig)
        if stopped or suppressed:
            lines.append("买入限制：" + ("；".join(reasons) if reasons else "存在被阻止的信号，未提供可确认的具体原因") + "。")
        else:
            lines.append("买入限制：未见日扫统一阻止，不代表全部执行检查已通过；仍需核对下一交易日价格、现金、整手及持仓限制。")
        lines.append("卖出限制：计划不等于成交；可卖股数、停牌、跌停及成交量仍可能阻止或延后执行。")
        events = [e for e in replay.get("order_events", []) if e.get("symbol") == code and str(e.get("date", ""))[:10] == today]
        for event in events:
            side = {"buy": "买入", "sell": "卖出"}.get(event.get("direction"), "未提供方向")
            lines.append(f"当日回放记录（{side}，不是下一交易日成交承诺）：{_explain(event.get('event'))}。")
            _trace(traces, f"{code} 当日回放记录", event)
    return lines


def render_daily_report(data: dict[str, Any], *, replay: dict[str, Any] | None = None,
                        symbols: dict[str, str] | None = None) -> str:
    """Explain producer fields without mutation, market access or new policy."""
    if not isinstance(data, dict):
        raise ValueError("daily report requires an object")
    mode = data.get("mode")
    if mode not in {"simulation", "account_decision_support"}:
        raise ValueError("unknown daily-report mode")
    account = mode == "account_decision_support"
    if not account and (data.get("status") != "ok" or data.get("risk_state_save_error")):
        raise ValueError("failed scan is not a successful report")
    traces: list[str] = []
    lines = _overview(data, account, traces)
    overview: list[tuple[str, str, str]] = []
    rows = (_account_rows(data, symbols or {}, traces, overview) if account
            else _simulation_rows(data, replay or {}, traces, overview))
    lines.extend(["", "## 逐股速览", "", "| 股票 | 代码 | 本次结论 |", "| --- | --- | --- |"])
    lines.extend(f"| {name} | {code} | {final} |" for name, code, final in overview)
    if not overview:
        lines.append("本次没有可展示的逐股记录，不能据此判断所有股票都没有风险。")
    lines.extend(["", "## 逐股结论与依据", "",
                  "先看每只股票标题中的处理结论，再看依据和限制。未提供的原因不作推断；多个策略记录不等于投票通过。", *rows])
    if not account:
        portfolio = data.get("portfolio", {})
        lines.extend(["", "## 历史回放背景（不是今天的盈亏或仓位建议）", "",
                      f"区间：{_text(data.get('start_date'))} 至 {_text(data.get('scan_date'))}。",
                      f"累计收益 {_number(portfolio.get('total_return'), percent=True)}；期间最大回撤 {_number(portfolio.get('max_drawdown'), percent=True)}。",
                      "这些历史统计不代表当前回撤，不能直接换算为今天应持有多少仓位。"])
        for event in (replay or {}).get("risk_events", [])[-5:]:
            lines.append(f"近期回放风险记录（{_text(event.get('date'))}）：{_explain(event.get('event'))}；历史事件不自动代表今天仍在生效。")
            _trace(traces, "近期回放风险记录", event)
    lines.extend(["", "## 下一可交易日复核", "",
                  "核对日期与账户是否一致，再看暂停买入及数据缺口；优先复核卖出和减仓风险。买入候选不是下单许可。",
                  "开盘前重新检查价格、可用现金、可卖股数、停牌与涨跌停；不要把计划卖出当成已经回笼现金。",
                  "", "## 原始依据（核对时阅读）", "",
                  "以下保留生产者的原始标签和记录。未知标签不会被自动解释为安全，也不参与重新决策。",
                  "当日执行与近期风险事件来自本次回放内存，补充在此，不是信号 JSON 的原有字段；来源 JSON 哈希仅校验该 JSON，不认证补充事件或正式经济验收。", "", *traces])
    # Keep fact paragraphs separate while preserving contiguous Markdown tables.
    return "\n".join(
        line + ("\n" if line and not line.startswith("|") else "")
        for line in lines
    ).rstrip() + "\n"


def _finite_json_number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("report source contains a non-finite number")
    return parsed


def publish_daily_report(source: Path, *, replay: dict[str, Any] | None = None,
                         symbols: dict[str, str] | None = None,
                         expected_identity: tuple[str, str] | None = None) -> Path | None:
    """Publish an optional reading copy AFTER the authoritative transaction.

    Failure cannot roll back or promote JSON/state. Content-addressed filenames
    and the full source digest distinguish reruns; there is no report pointer.
    """
    temporary: str | None = None
    try:
        raw = source.read_bytes()
        data = json.loads(raw, parse_constant=_finite_json_number, parse_float=_finite_json_number)
        if expected_identity and data.get(expected_identity[0]) != expected_identity[1]:
            raise ValueError("source was replaced by another run")
        report = render_daily_report(data, replay=replay, symbols=symbols)
        digest = hashlib.sha256(raw).hexdigest()
        output = source.with_name(f"{source.stem}.{digest[:12]}.md")
        print(report.split("## 原始依据", 1)[0])
        content = (report + f"\n来源文件：{_text(source.name)}\n\n来源文件 SHA-256：{digest}\n"
                   "\n此页为说明副本，不是成功索引。先确认本次命令成功、日期正确且来源文件哈希匹配；旧报告不代表本次运行成功。\n")
        fd, temporary = tempfile.mkstemp(dir=source.parent, prefix=".report_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        temporary = None
        print(f"阅读版已保存：{output}")
        return output
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"阅读版未保存：{exc}。机器结果与风险状态不因阅读版失败而改写；请检查本次 JSON，不要误用旧报告。")
        return None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
