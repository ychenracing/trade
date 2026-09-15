"""Shared close-known AB5 budget; planning stress is not a guaranteed loss bound."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

import pandas as pd
from typing import Any

from quantfusion.domain.rules import floor_to_lot, limit_pct_for_code, require_finite, require_int
from quantfusion.domain.models import Signal
from quantfusion.config.overlay import (
    RISK_ACTION_PRIORITY,
    SYMBOL_SUB_INDUSTRY,
)
from quantfusion.execution.c6_receipts import reconcile_close_queue
from quantfusion.risk.overlay.adapter import apply_risk_actions
from quantfusion.risk.overlay.models import RiskAction
from quantfusion.strategy.trend import EARLY_DUAL_TRANSITION_RSI_MAX


def account_budget_capacity(
    equity: float, peak: float, cfg: Mapping[str, Any], book_count: int,
) -> dict[str, float]:
    """Use the continuous account HWM, two stress sessions and exit-cost reserve."""
    equity = require_finite('account equity', equity, min_value=0.)
    peak = require_finite('account lifetime peak', peak, min_value=0.01)
    if equity > peak + 1e-8:
        raise ValueError('account lifetime peak must include current equity')
    book_count = require_int('book_count', book_count, min_value=0)
    daily = require_finite('daily_loss_limit', cfg['daily_loss_limit'],
                           min_value=0.000001, max_value=1., inclusive_max=False)
    costs = {key: require_finite(key, cfg[key], min_value=0.)
             for key in ('slippage', 'commission_rate', 'stamp_duty', 'min_commission')}
    maximum = require_finite('max_total_weight', cfg['max_total_weight'],
                              min_value=0., max_value=1.)
    floor = 0.82 * peak
    stress = 1. - (1. - daily)**2
    cost_rate = 2*costs['slippage'] + 2*costs['commission_rate'] + costs['stamp_duty']
    fixed = 2*book_count*costs['min_commission']
    budget = max(0., equity - floor - fixed)
    ordinary_cap = maximum * equity
    return {'equity': equity, 'lifetime_peak': peak, 'floor': floor,
            'stress_fraction': stress, 'cost_rate': cost_rate, 'fixed_cost_reserve': fixed,
            'remaining_loss_budget': budget, 'ordinary_gross_cap': ordinary_cap,
            'gross_cap': min(ordinary_cap, budget/(stress+cost_rate))}


def plan_account_risk_budget(
    equity: float, peak: float, cfg: Mapping[str, Any],
    books: Sequence[tuple[int, str, str, int, float]],
    buys: Sequence[tuple[int, Signal, float]],
    score: Callable[[str], float], *, date_str: str,
    stress_by_symbol: Mapping[str, float] | None = None,
    direct_loss_by_symbol: Mapping[str, float] | None = None,
    shock_episode_active: bool = False,
    shock_floor: float = 0.,
    preserve_strategy_valid_holdings: bool = False,
    risk_alert_active: bool = False,
    weak_book_ids: set[tuple[int, str, str]] | None = None,
    strategy_handoff_symbols: set[str] | None = None,
    repeated_proven_reentry_symbols: set[str] | None = None,
    proven_early_dual_book_ids: set[tuple[int, str, str]] | None = None,
    shock_reduced_book_ids: set[tuple[int, str, str]] | None = None,
    confirmed_shock_reduced_book_ids: set[tuple[int, str, str]] | None = None,
    crowded_shock_reduced_book_ids: set[tuple[int, str, str]] | None = None,
) -> tuple[dict[str, Any], list[RiskAction]]:
    """Plan the same AB5 reductions for real snapshot books or replay books.

    No portfolio, cash, order queue or history is created or mutated here.
    Existing/queued sells provide no buying credit. Callers retain their own
    execution adapter and must not treat these close-known plans as fills.
    """
    cfg = dict(cfg)
    book_ids = {(state, symbol, strategy) for state, symbol, strategy, shares, _ in books if shares}
    book_ids.update((state, signal.symbol, signal.strategy_name)
                    for state, signal, _ in buys if signal.target_shares)
    receipt = account_budget_capacity(equity, peak, cfg, len(book_ids))
    gross = sum(shares*price for _, _, _, shares, price in books)
    if gross > equity + 1e-8:
        raise ValueError("account budget cannot certify leveraged/negative-cash books")
    candidates = dict(stress_by_symbol or {})
    direct_losses = dict(direct_loss_by_symbol or {})
    if any(
        not isinstance(symbol, str)
        or not symbol
        or not math.isfinite(float(loss))
        or float(loss) < float(cfg['daily_loss_limit'])
        or symbol not in candidates
        or float(loss) > float(candidates[symbol]) + 1e-12
        for symbol, loss in direct_losses.items()
    ):
        raise ValueError(
            'direct-loss evidence must be close-known shocked symbols at or above daily loss limit'
        )
    if type(shock_episode_active) is not bool:
        raise ValueError('shock episode state must be boolean')
    if type(preserve_strategy_valid_holdings) is not bool:
        raise ValueError('holding-preservation state must be boolean')
    if type(risk_alert_active) is not bool:
        raise ValueError('account risk alert state must be boolean')
    weak_books = set(weak_book_ids or ())
    handoff_symbols = set(strategy_handoff_symbols or ())
    if any(not isinstance(symbol, str) or not symbol for symbol in handoff_symbols):
        raise ValueError('strategy handoff symbols must be non-empty strings')
    repeated_symbols = set(repeated_proven_reentry_symbols or ())
    if any(not isinstance(symbol, str) or not symbol for symbol in repeated_symbols):
        raise ValueError('repeated proven reentry symbols must be non-empty strings')
    proven_dual_books = set(proven_early_dual_book_ids or ())
    shock_reduced_books = set(shock_reduced_book_ids or ())
    confirmed_shock_reduced_books = set(
        confirmed_shock_reduced_book_ids or ()
    )
    crowded_shock_reduced_books = set(crowded_shock_reduced_book_ids or ())
    for label, ids in (
        ('proven early dual', proven_dual_books),
        ('shock-reduced', shock_reduced_books),
        ('confirmed shock-reduced', confirmed_shock_reduced_books),
        ('crowded shock-reduced', crowded_shock_reduced_books),
    ):
        if any(
            not isinstance(book_id, tuple)
            or len(book_id) != 3
            or not isinstance(book_id[0], int)
            or not isinstance(book_id[1], str)
            or not book_id[1]
            or not isinstance(book_id[2], str)
            or not book_id[2]
            for book_id in ids
        ):
            raise ValueError(f'{label} book ids must be (state, symbol, strategy)')
    shock_floor = require_finite('shock equity floor', shock_floor,
                                 min_value=0., max_value=peak)
    shocked_groups = {SYMBOL_SUB_INDUSTRY.get(symbol, symbol) for symbol in candidates}
    group_gross: dict[str, float] = {}
    for _, symbol, _, shares, price in books:
        group = SYMBOL_SUB_INDUSTRY.get(symbol, symbol)
        group_gross[group] = group_gross.get(group, 0.) + shares * price
    dominant_group_fraction = max(group_gross.values(), default=0.) / gross if gross else 0.
    held_symbol_count = len({
        symbol for _, symbol, _, shares, _ in books if shares
    })
    drawdown = max(0., (peak - equity) / peak)
    shock_trigger = (
        bool(candidates)
        and drawdown >= float(cfg['daily_loss_limit'])
        and len(shocked_groups) >= 2
        and max(candidates.values()) >= abs(float(cfg['cm_risk_severe_direct_return']))
        and dominant_group_fraction >= 1. - float(cfg['sector_shock_breadth'])
    )
    shock_confirmed = shock_trigger and not shock_episode_active
    effective_floor = max(
        receipt['floor'],
        shock_floor if shock_confirmed or risk_alert_active else 0.,
    )
    receipt['effective_policy_floor'] = effective_floor
    receipt['remaining_loss_budget'] = max(
        0., equity - effective_floor - receipt['fixed_cost_reserve'])
    board_cap = max((limit_pct_for_code(symbol, cfg) for symbol in (
        {book[1] for book in books} | set(candidates)
    )), default=receipt['stress_fraction'])
    systemic_stress = (
        min(board_cap, max(receipt['stress_fraction'], 2. * drawdown,
                           max(candidates.values())))
        if shock_confirmed else receipt['stress_fraction']
    )
    systemic_stress_fraction = systemic_stress + receipt['cost_rate']
    receipt['systemic_stress_fraction'] = systemic_stress_fraction
    receipt['gross_cap'] = min(
        receipt['ordinary_gross_cap'],
        receipt['remaining_loss_budget']
        / systemic_stress_fraction,
    )
    cap = receipt["gross_cap"]
    requested = sum(value for _, _, value in buys)
    binding = cap < receipt["ordinary_gross_cap"] - 1e-8
    gross_scale = min(1., max(0., cap-gross)/requested) if binding and requested else 1.
    base_gap_rate = receipt["stress_fraction"] + receipt["cost_rate"]
    held_group_gap: dict[str, float] = {}
    buy_group_gap: dict[str, float] = {}
    held_group_base: dict[str, float] = {}
    buy_group_base: dict[str, float] = {}
    for _, symbol, _, shares, price in books:
        group = SYMBOL_SUB_INDUSTRY.get(symbol, symbol)
        value = shares * price
        board_rate = limit_pct_for_code(symbol, cfg) + receipt["cost_rate"]
        held_group_gap[group] = (
            held_group_gap.get(group, 0.) + value * board_rate
        )
        held_group_base[group] = held_group_base.get(group, 0.) + value * base_gap_rate
    for _, signal, value in buys:
        group = SYMBOL_SUB_INDUSTRY.get(signal.symbol, signal.symbol)
        board_rate = limit_pct_for_code(signal.symbol, cfg) + receipt["cost_rate"]
        buy_group_gap[group] = (
            buy_group_gap.get(group, 0.) + value * board_rate
        )
        buy_group_base[group] = buy_group_base.get(group, 0.) + value * base_gap_rate

    def grouped_gap_debit(scale: float) -> float:
        groups = set(held_group_gap) | set(buy_group_gap)
        market_scenario = base_gap_rate * (gross + scale * requested)
        excess_group_scenario = max(
            (
                held_group_gap.get(group, 0.)
                + scale * buy_group_gap.get(group, 0.)
                - held_group_base.get(group, 0.)
                - scale * buy_group_base.get(group, 0.)
                for group in groups
            ),
            default=0.,
        )
        return market_scenario + excess_group_scenario

    current_gap = grouped_gap_debit(0.)
    full_gap = grouped_gap_debit(1.)
    buy_gap = max(0., full_gap - current_gap)
    gap_scale = 1.
    if binding and requested and full_gap > receipt["remaining_loss_budget"]:
        low, high = 0., 1.
        for _ in range(60):
            middle = (low + high) / 2.
            if grouped_gap_debit(middle) <= receipt["remaining_loss_budget"]:
                low = middle
            else:
                high = middle
        gap_scale = low
    shock_episode = bool(candidates) and (shock_confirmed or shock_episode_active)
    reduction_suspended = bool(candidates) and shock_episode_active
    observed = candidates if shock_confirmed else {}
    stresses = {symbol: (systemic_stress_fraction if observed else
                         receipt['stress_fraction'] + receipt['cost_rate'])
        for symbol in {book[1] for book in books} | {signal.symbol for _, signal, _ in buys}}
    held_stress = sum(shares * price * stresses[symbol] for _, symbol, _, shares, price in books)
    buy_stress = sum(value * systemic_stress_fraction for _, signal, value in buys)
    shock_scale = 0. if shock_episode and buy_stress else 1.
    stress_relief = max(0., held_stress - receipt['remaining_loss_budget']) if observed else 0.
    ordinary_buy_scale = min(gross_scale, gap_scale)
    # An alert governs the amount of new risk; it is not a permanent entry
    # lock.  Once prior reductions have actually filled, admit only the risk
    # that fits the live close-known cap.  ``gross`` deliberately still
    # includes books with queued sells, so an unfilled exit creates no credit.
    alert_buy_scale = (
        min(gross_scale, gap_scale) if risk_alert_active and requested else 1.
    )
    residual_books = [
        book for book in books if book[:3] not in weak_books
    ]
    residual_gross = sum(shares * price for _, _, _, shares, price in residual_books)
    residual_symbol_gross: dict[str, float] = {}
    for _, symbol, _, shares, price in residual_books:
        residual_symbol_gross[symbol] = (
            residual_symbol_gross.get(symbol, 0.) + shares * price
        )
    dominant_symbol = (
        min(
            residual_symbol_gross,
            key=lambda symbol: (-residual_symbol_gross[symbol], symbol),
        )
        if residual_symbol_gross else None
    )
    dominant_book_fraction = (
        residual_symbol_gross[dominant_symbol] / residual_gross
        if dominant_symbol is not None and residual_gross else 0.
    )
    dominant_fraction = (
        residual_symbol_gross[dominant_symbol] / equity
        if dominant_symbol is not None and equity else 0.
    )
    symbol_limit = require_finite(
        'max_symbol_weight', cfg['max_symbol_weight'], min_value=0., max_value=1.,
    )
    daily_loss_limit = require_finite(
        'daily_loss_limit', cfg['daily_loss_limit'], min_value=0.000001,
        max_value=1., inclusive_max=False,
    )
    alert_dominant_equity_weight_breach = bool(
        risk_alert_active
        and dominant_symbol is not None
        and dominant_fraction > symbol_limit
    )
    # During an account alert, invested-book concentration is a separate risk
    # from the ordinary equity-weight limit.  Act on it only when the live book
    # exceeds its close-known loss cap by more than a full daily-loss allowance;
    # this keeps a cash-heavy, modestly over-cap winner from being mistaken for
    # an account-weight breach while still reducing a dominant book at the
    # first materially unsafe node.
    alert_dominant_invested_concentration = bool(
        risk_alert_active
        and dominant_symbol is not None
        and dominant_book_fraction > symbol_limit
        and residual_gross - cap > equity * daily_loss_limit
    )
    alert_dominant_symbol = (
        dominant_symbol
        if (
            alert_dominant_equity_weight_breach
            or alert_dominant_invested_concentration
        )
        else None
    )
    actions = []
    relief = (
        0.
        if preserve_strategy_valid_holdings and not shock_confirmed
        else max(0., gross-cap)
    )
    if reduction_suspended:
        pass
    elif shock_confirmed and gross:
        # Keep group-propagated stress for shock confirmation and the required
        # account-level relief, but do not lose the identity of holdings that
        # actually breached the close-known daily-loss threshold. When those
        # direct-hit books can fund the same required relief, source it there
        # before touching correlated holdings that did not themselves breach.
        retain_scale = min(
            1.,
            cap / gross,
            receipt['remaining_loss_budget'] / held_stress if held_stress else 1.,
        )
        required_relief = gross * (1. - retain_scale)
        ordered_books = sorted(books, key=lambda book: (book[1], book[0], book[2]))

        def append_pro_rata_relief(
            cohort: Sequence[tuple[int, str, str, int, float]], target: float,
        ) -> float:
            cohort_gross = sum(shares * price for _, _, _, shares, price in cohort)
            if not cohort_gross or target <= 0.:
                return 0.
            fraction = min(1., target / cohort_gross)
            planned = 0.
            for state, symbol, strategy, shares, price in cohort:
                reduction = min(
                    shares, math.ceil(shares * fraction / 100.) * 100,
                )
                if not reduction:
                    continue
                actions.append(RiskAction(
                    symbol, strategy, reduction, price, date_str,
                    "account_budget_trim", RISK_ACTION_PRIORITY["account_budget_trim"],
                    state_index=state,
                ))
                planned += reduction * price
            return planned

        if direct_losses:
            direct_books = [book for book in ordered_books if book[1] in direct_losses]
            residual_books = [book for book in ordered_books if book[1] not in direct_losses]
            direct_capacity = sum(
                shares * price for _, _, _, shares, price in direct_books
            )
            planned_relief = append_pro_rata_relief(
                direct_books, min(required_relief, direct_capacity),
            )
            residual_relief = max(0., required_relief - planned_relief)
            append_pro_rata_relief(residual_books, residual_relief)
        else:
            append_pro_rata_relief(ordered_books, required_relief)
    elif risk_alert_active:
        for state, symbol, strategy, shares, price in sorted(
            books, key=lambda book: (book[1], book[0], book[2]),
        ):
            if (state, symbol, strategy) in weak_books:
                actions.append(RiskAction(
                    symbol, strategy, shares, price, date_str,
                    "account_budget_trim", RISK_ACTION_PRIORITY["account_budget_trim"],
                    state_index=state,
                ))
        if alert_dominant_symbol is not None:
            dominant_books = sorted(
                (book for book in residual_books if book[1] == alert_dominant_symbol),
                key=lambda book: (book[0], book[2]),
            )
            dominant_shares = sum(book[3] for book in dominant_books)
            dominant_value = residual_symbol_gross[alert_dominant_symbol]
            equity_weight_relief = max(
                0., dominant_value - equity * symbol_limit,
            )
            invested_concentration_relief = (
                max(0., residual_gross - cap)
                if alert_dominant_invested_concentration else 0.
            )
            reduction_value = max(
                equity_weight_relief, invested_concentration_relief,
            )
            target_shares = min(
                dominant_shares,
                math.ceil(reduction_value / dominant_books[0][4] / 100.) * 100,
            )
            reductions = [
                floor_to_lot(target_shares * book[3] / dominant_shares)
                for book in dominant_books
            ]
            unassigned = target_shares - sum(reductions)
            for index, book in enumerate(dominant_books):
                if unassigned <= 0:
                    break
                room = book[3] - reductions[index]
                extra = min(room, unassigned, 100)
                reductions[index] += extra
                unassigned -= extra
            for (state, symbol, strategy, _, price), reduction in zip(
                dominant_books, reductions, strict=True,
            ):
                if reduction:
                    actions.append(RiskAction(
                        symbol, strategy, reduction, price, date_str,
                        "account_budget_trim",
                        RISK_ACTION_PRIORITY["account_budget_trim"],
                        state_index=state,
                    ))
    else:
        for state, symbol, strategy, shares, price in sorted(
            books, key=lambda book: (score(book[1]), book[1], book[0], book[2]),
        ):
            reduction = min(
                shares,
                math.ceil(max(relief/price, stress_relief/price/stresses[symbol])/100.)*100,
            )
            if not reduction:
                continue
            actions.append(RiskAction(
                symbol, strategy, reduction, price, date_str,
                "account_budget_trim", RISK_ACTION_PRIORITY["account_budget_trim"],
                state_index=state,
            ))
            relief = max(0., relief-reduction*price)
            stress_relief = max(0., stress_relief - reduction * price * stresses[symbol])
    held_groups = {
        SYMBOL_SUB_INDUSTRY.get(symbol, symbol) for _, symbol, _, shares, _ in books
        if shares
    }
    portfolio_max_positions = require_int(
        'portfolio max positions', cfg['max_positions'], min_value=1,
    )
    crowded_portfolio = held_symbol_count >= portfolio_max_positions - 1
    quality_admission_capacity_available = gross <= cap + 1e-8
    quality_admission_risk_state_available = bool(
        quality_admission_capacity_available
        or drawdown < require_finite(
            'level-3 drawdown threshold', cfg['cm_risk_level3_drawdown'],
            min_value=0., max_value=1., inclusive_max=False,
        )
    )
    durable_breakout_admitted = [
        bool(
            quality_admission_risk_state_available
            and preserve_strategy_valid_holdings
            and not risk_alert_active
            and not shock_episode
            and signal.fusion_votes >= 2
            and signal.strategy_name == 'atr_channel'
            and SYMBOL_SUB_INDUSTRY.get(signal.symbol, signal.symbol)
            not in held_groups
        )
        for _, signal, _ in buys
    ]
    handoff_admitted = [
        bool(
            quality_admission_risk_state_available
            and preserve_strategy_valid_holdings
            and not risk_alert_active
            and not shock_episode
            and signal.strategy_name == 'dual_ma'
            and signal.symbol in handoff_symbols
        )
        for _, signal, _ in buys
    ]
    repeated_reentry_admitted = [
        bool(
            quality_admission_risk_state_available
            and preserve_strategy_valid_holdings
            and not risk_alert_active
            and not shock_episode
            and signal.fusion_votes >= 2
            and signal.strategy_name == 'turtle_breakout'
            and signal.symbol in repeated_symbols
            and limit_pct_for_code(signal.symbol, cfg) <= receipt['stress_fraction']
            and SYMBOL_SUB_INDUSTRY.get(signal.symbol, signal.symbol)
            not in held_groups
        )
        for _, signal, _ in buys
    ]
    proven_dual_admitted = [
        bool(
            quality_admission_risk_state_available
            and preserve_strategy_valid_holdings
            and not risk_alert_active
            and not shock_episode
            and signal.strategy_name == 'dual_ma'
            and (state, signal.symbol, signal.strategy_name) in proven_dual_books
        )
        for state, signal, _ in buys
    ]
    alert_proven_dual = [
        bool(
            quality_admission_capacity_available
            and preserve_strategy_valid_holdings
            and risk_alert_active
            and not shock_episode
            and signal.strategy_name == 'dual_ma'
            and (state, signal.symbol, signal.strategy_name) in proven_dual_books
        )
        for state, signal, _ in buys
    ]
    shock_reduced_pyramid = [
        bool(
            (state, signal.symbol, signal.strategy_name) in shock_reduced_books
            and 'pyramid' in str(signal.reason).lower()
            and (
                (state, signal.symbol, signal.strategy_name)
                in confirmed_shock_reduced_books
                or (state, signal.symbol, signal.strategy_name)
                in crowded_shock_reduced_books
            )
        )
        for state, signal, _ in buys
    ]
    shock_reduced_pyramid_books = [
        (state, signal.symbol, signal.strategy_name)
        for (state, signal, _), blocked in zip(
            buys, shock_reduced_pyramid, strict=True,
        )
        if blocked
    ]
    quality_admitted = [
        not blocked and (durable or handoff or repeated or proven_dual)
        for durable, handoff, repeated, proven_dual, blocked in zip(
            durable_breakout_admitted, handoff_admitted,
            repeated_reentry_admitted, proven_dual_admitted,
            shock_reduced_pyramid,
            strict=True,
        )
    ]
    base_buy_scale = min(ordinary_buy_scale, shock_scale, alert_buy_scale)
    buy_scales = [
        (
            0.
            if blocked
            else 1.
            if admitted
            else gross_scale
            if alert_proven
            else base_buy_scale
        )
        for admitted, alert_proven, blocked in zip(
            quality_admitted,
            alert_proven_dual,
            shock_reduced_pyramid,
            strict=True,
        )
    ]
    return {**receipt, "gross_before": gross, "buy_envelope_binding": binding,
            "buy_gross_scale": gross_scale, "current_gap_debit": current_gap,
            "requested_buy_gap_debit": buy_gap, "buy_gap_scale": gap_scale,
            "observed_shock_candidates": candidates,
            "observed_shock_confirmed": shock_confirmed,
            "shocked_group_count": len(shocked_groups),
            "dominant_group_fraction": dominant_group_fraction,
            "shock_episode_active": shock_episode,
            "reduction_suspended_during_episode": reduction_suspended,
            "strategy_valid_holdings_preserved": (
                preserve_strategy_valid_holdings
                and not shock_confirmed
                and not risk_alert_active
            ),
            "risk_alert_active": risk_alert_active,
            "weak_book_ids": sorted(weak_books),
            "strategy_handoff_symbols": sorted(handoff_symbols),
            "repeated_proven_reentry_symbols": sorted(repeated_symbols),
            "proven_early_dual_book_ids": sorted(proven_dual_books),
            "shock_reduced_book_ids": sorted(shock_reduced_books),
            "confirmed_shock_reduced_book_ids": sorted(
                confirmed_shock_reduced_books
            ),
            "crowded_shock_reduced_book_ids": sorted(
                crowded_shock_reduced_books
            ),
            "held_symbol_count": held_symbol_count,
            "portfolio_max_positions": portfolio_max_positions,
            "crowded_portfolio": crowded_portfolio,
            "alert_dominant_symbol": alert_dominant_symbol,
            "alert_dominant_equity_weight_breach": (
                alert_dominant_equity_weight_breach
            ),
            "alert_dominant_invested_concentration": (
                alert_dominant_invested_concentration
            ),
            "alert_dominant_fraction": dominant_fraction,
            "alert_dominant_invested_fraction": dominant_book_fraction,
            "observed_shock_stress": dict(observed), "held_stress_debit": held_stress,
            "buy_shock_scale": shock_scale,
            "quality_admission_capacity_available": (
                quality_admission_capacity_available
            ),
            "quality_admission_risk_state_available": (
                quality_admission_risk_state_available
            ),
            "quality_admitted_buy_indexes": [
                index for index, admitted in enumerate(quality_admitted) if admitted
            ],
            "handoff_admitted_buy_indexes": [
                index for index, admitted in enumerate(handoff_admitted) if admitted
            ],
            "repeated_reentry_admitted_buy_indexes": [
                index for index, admitted in enumerate(repeated_reentry_admitted)
                if admitted
            ],
            "proven_dual_admitted_buy_indexes": [
                index for index, admitted in enumerate(proven_dual_admitted)
                if admitted
            ],
            "alert_proven_dual_buy_indexes": [
                index for index, admitted in enumerate(alert_proven_dual)
                if admitted
            ],
            "shock_reduced_pyramid_buy_indexes": [
                index for index, blocked in enumerate(shock_reduced_pyramid)
                if blocked
            ],
            "shock_reduced_pyramid_book_ids": shock_reduced_pyramid_books,
            "buy_scales": buy_scales,
            "buy_scale": min(buy_scales, default=base_buy_scale)}, actions


def observed_direct_losses(
    frames: Mapping[str, pd.DataFrame], date: pd.Timestamp, cfg: Mapping[str, Any],
) -> dict[str, float]:
    """Return symbols whose own close-known loss breached the existing limit."""
    direct: dict[str, float] = {}
    for symbol, frame in frames.items():
        closes = frame.loc[frame.index <= date, 'close'].tail(3)
        if len(closes) < 3:
            continue
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in closes):
            raise ValueError('observed shock requires finite positive closes')
        loss = max(0., -float(closes.pct_change().min()))
        if loss >= float(cfg['daily_loss_limit']):
            direct[symbol] = loss
    return direct


def observed_shock_stress(
    frames: Mapping[str, pd.DataFrame], date: pd.Timestamp, cfg: Mapping[str, Any],
) -> dict[str, float]:
    """Reserve board shock exposure after an observed industry-member selloff.

    The two completed return observations match the budget's two-session
    planning horizon. Future bars never enter the calculation. This is a
    planning estimate, not a guarantee that a later sell can execute.
    """
    shocked: dict[str, float] = {}
    for symbol, loss in observed_direct_losses(frames, date, cfg).items():
        group = SYMBOL_SUB_INDUSTRY.get(symbol, symbol)
        shocked[group] = max(shocked.get(group, 0.), loss)
    return {symbol: shocked[SYMBOL_SUB_INDUSTRY.get(symbol, symbol)]
            for symbol in frames
            if SYMBOL_SUB_INDUSTRY.get(symbol, symbol) in shocked}


def apply_account_risk_budget(
    states: Sequence[Any], date: pd.Timestamp, assets: float, peak: float,
    cfg: Mapping[str, Any], score: Callable[[str], float], events: list[dict[str, Any]],
    *, shock_floor: float = 0., preserve_strategy_valid_holdings: bool = False,
    risk_alert_active: bool | None = None,
    portfolio_evidence_buy_symbols: set[str] | None = None,
) -> None:
    """Adapt the shared plan to the existing replay books and order queues."""
    if risk_alert_active is None:
        latest_alert = next((
            event for event in reversed(events)
            if event.get('event') in {
                'portfolio_drawdown_alert_on', 'portfolio_drawdown_alert_off',
            }
            and event.get('sleeve') in {None, 'portfolio'}
        ), None)
        risk_alert_active = bool(
            latest_alert
            and latest_alert.get('event') == 'portfolio_drawdown_alert_on'
        )
    date_str = date.strftime("%Y-%m-%d")
    evidence_symbols = (
        None
        if portfolio_evidence_buy_symbols is None
        else set(portfolio_evidence_buy_symbols)
    )
    if evidence_symbols is not None and any(
        not isinstance(symbol, str) or not symbol for symbol in evidence_symbols
    ):
        raise ValueError(
            'portfolio evidence buy symbols must be non-empty strings'
        )
    previous_handoff_symbols = set(next((
        event.get('strategy_handoff_symbols', ())
        for event in reversed(events)
        if event.get('event') == 'account_budget_envelope'
    ), ()))
    books, buys = [], []
    buy_slots: list[tuple[int, int]] = []
    excluded_buy_slots: list[tuple[int, int]] = []
    weak_book_ids: set[tuple[int, str, str]] = set()
    protected_handoff_book_ids: set[tuple[int, str, str]] = set()
    protected_proven_dual_book_ids: set[tuple[int, str, str]] = set()
    repeated_proven_reentry_symbols: set[str] = set()
    proven_early_dual_book_ids: set[tuple[int, str, str]] = set()
    shock_reduced_book_ids: set[tuple[int, str, str]] = set()
    confirmed_shock_reduced_book_ids: set[tuple[int, str, str]] = set()
    crowded_shock_reduced_book_ids: set[tuple[int, str, str]] = set()
    observed_shock_reduction_dates = {
        str(event['date'])
        for event in events
        if event.get('event') == 'account_budget_envelope'
        and event.get('observed_shock_confirmed') is True
        and event.get('new_reduction_orders', 0)
        and event.get('date')
    }
    crowded_shock_reduction_dates = {
        str(event['date'])
        for event in events
        if str(event.get('date', '')) in observed_shock_reduction_dates
        and event.get('crowded_portfolio') is True
    }
    strategy_handoff_symbols = {
        trade.symbol
        for state in states
        for trade in state.sleeve.trades
        if trade.date == date_str
        and trade.direction == 'sell'
        and trade.strategy_name.split(':')[-1] == 'turtle_breakout'
        and trade.reason.startswith('Donchian exit')
    }
    costs = dict(cfg)
    for state_index, state in enumerate(states):
        for key in ("slippage", "commission_rate", "stamp_duty", "min_commission"):
            costs[key] = max(costs[key], state.sleeve.cfg[key])
        cycle_shares: dict[tuple[str, str], int] = {}
        confirmed_live_cycles: set[tuple[str, str]] = set()
        shock_reduced_live_cycles: set[tuple[str, str]] = set()
        confirmed_shock_reduced_live_cycles: set[tuple[str, str]] = set()
        crowded_shock_reduced_live_cycles: set[tuple[str, str]] = set()
        for trade in state.sleeve.trades:
            if trade.date > date_str:
                continue
            strategy_name = trade.strategy_name.split(':')[-1]
            cycle_id = (trade.symbol, strategy_name)
            quantity = require_int(
                'historical cycle shares', trade.shares, min_value=1,
            )
            before = cycle_shares.get(cycle_id, 0)
            if trade.direction == 'buy':
                if before == 0:
                    shock_reduced_live_cycles.discard(cycle_id)
                    confirmed_shock_reduced_live_cycles.discard(cycle_id)
                    crowded_shock_reduced_live_cycles.discard(cycle_id)
                    if trade.reason.startswith((
                        '[two-strategy confirmation]',
                        '[three-strategy confirmation]',
                    )):
                        confirmed_live_cycles.add(cycle_id)
                    else:
                        confirmed_live_cycles.discard(cycle_id)
                cycle_shares[cycle_id] = before + quantity
                continue
            remaining = max(0, before - quantity)
            cycle_shares[cycle_id] = remaining
            if remaining == 0:
                shock_reduced_live_cycles.discard(cycle_id)
                confirmed_shock_reduced_live_cycles.discard(cycle_id)
                crowded_shock_reduced_live_cycles.discard(cycle_id)
                confirmed_live_cycles.discard(cycle_id)
            elif (
                trade.reason == 'account_budget_trim'
                and trade.signal_date in observed_shock_reduction_dates
            ):
                shock_reduced_live_cycles.add(cycle_id)
                if cycle_id in confirmed_live_cycles:
                    confirmed_shock_reduced_live_cycles.add(cycle_id)
                if trade.signal_date in crowded_shock_reduction_dates:
                    crowded_shock_reduced_live_cycles.add(cycle_id)
        latest_atr_buy: dict[str, Any] = {}
        counted_atr_entries: set[tuple[str, str]] = set()
        proven_cycle_counts: dict[str, int] = {}
        completed_proven_atr_symbols: set[str] = set()
        for trade in state.sleeve.trades:
            if trade.date > date_str:
                continue
            if trade.strategy_name.split(':')[-1] != 'atr_channel':
                continue
            if trade.direction == 'buy':
                latest_atr_buy[trade.symbol] = trade
                continue
            if trade.direction != 'sell' or trade.symbol not in latest_atr_buy:
                continue
            entry_trade = latest_atr_buy[trade.symbol]
            shares = require_int('historical sell shares', trade.shares, min_value=1)
            entry_basis = (
                require_finite('historical net proceeds', trade.net_cash_flow)
                - require_finite('historical realized pnl', trade.pnl)
            ) / shares
            peak_close = require_finite(
                'historical peak close', trade.peak_close, min_value=0.000001,
            )
            cycle_id = (trade.symbol, entry_trade.date)
            if (
                cycle_id not in counted_atr_entries
                and entry_trade.reason.startswith('[two-strategy confirmation]')
                and entry_basis > 0.
                and peak_close
                >= entry_basis * (1. + limit_pct_for_code(trade.symbol, costs)) ** 2
            ):
                counted_atr_entries.add(cycle_id)
                proven_cycle_counts[trade.symbol] = (
                    proven_cycle_counts.get(trade.symbol, 0) + 1
                )
            if (
                require_finite('historical realized pnl', trade.pnl) > 0.
                and entry_basis > 0.
                and peak_close
                >= entry_basis * (1. + limit_pct_for_code(trade.symbol, costs)) ** 2
            ):
                completed_proven_atr_symbols.add(trade.symbol)
        repeated_proven_reentry_symbols.update(
            symbol for symbol, count in proven_cycle_counts.items() if count >= 2
        )
        live_strategies = {
            symbol: {
                strategy
                for strategy, position in positions.items()
                if require_int('held shares', position.shares, min_value=0)
            }
            for symbol, positions in state.sleeve.positions.items()
        }
        shock_reduced_book_ids.update(
            (state_index, symbol, strategy)
            for symbol, strategies in live_strategies.items()
            for strategy in strategies
            if (symbol, strategy) in shock_reduced_live_cycles
        )
        confirmed_shock_reduced_book_ids.update(
            (state_index, symbol, strategy)
            for symbol, strategies in live_strategies.items()
            for strategy in strategies
            if (symbol, strategy) in confirmed_shock_reduced_live_cycles
        )
        crowded_shock_reduced_book_ids.update(
            (state_index, symbol, strategy)
            for symbol, strategies in live_strategies.items()
            for strategy in strategies
            if (symbol, strategy) in crowded_shock_reduced_live_cycles
        )

        def indicator_at_signal(
            symbol: str, name: str, signal_date: str,
        ) -> float | None:
            series = getattr(state, 'indicator_map', {}).get(symbol, {}).get(name)
            if series is None:
                return None
            available = series.loc[
                series.index <= pd.Timestamp(signal_date)
            ].dropna()
            if available.empty:
                return None
            return require_finite(f'{name} at signal', available.iloc[-1])

        for symbol, positions in sorted(state.sleeve.positions.items()):
            for strategy, position in sorted(positions.items()):
                shares = require_int("held shares", position.shares, min_value=0)
                if not shares:
                    continue
                frame = state.data_map.get(symbol)
                if frame is None:
                    raise ValueError("account budget requires every held mark")
                price = require_finite("held close", state.sleeve._latest_close_on_or_before(frame, date), min_value=0.000001)
                books.append((state_index, symbol, strategy, shares, price))
                if risk_alert_active:
                    indicators = getattr(state, 'indicator_map', {}).get(symbol, {})
                    short_ma = indicators.get('ma_short')
                    available = (
                        short_ma.loc[short_ma.index <= date].dropna()
                        if short_ma is not None else pd.Series(dtype=float)
                    )
                    if available.empty:
                        raise ValueError(
                            'account risk alert requires a close-known short moving average'
                        )
                    ma_value = require_finite(
                        'held short moving average', available.iloc[-1], min_value=0.000001,
                    )
                    entry = require_finite(
                        'held entry price', position.entry_price, min_value=0.000001,
                    )
                    fresh_handoff = (
                        strategy == 'dual_ma'
                        and position.entry_date == date_str
                        and symbol in previous_handoff_symbols
                    )
                    fresh_dual_trade = next((
                        trade
                        for trade in reversed(state.sleeve.trades)
                        if trade.date == date_str
                        and trade.direction == 'buy'
                        and trade.symbol == symbol
                        and trade.strategy_name.split(':')[-1] == 'dual_ma'
                    ), None)
                    fresh_dual_rsi = (
                        indicator_at_signal(
                            symbol, 'rsi', fresh_dual_trade.signal_date,
                        )
                        if fresh_dual_trade is not None
                        and fresh_dual_trade.signal_date is not None
                        else None
                    )
                    fresh_proven_dual = (
                        strategy == 'dual_ma'
                        and position.entry_date == date_str
                        and symbol in completed_proven_atr_symbols
                        and fresh_dual_rsi is not None
                        and fresh_dual_rsi <= EARLY_DUAL_TRANSITION_RSI_MAX
                    )
                    if fresh_handoff:
                        protected_handoff_book_ids.add((state_index, symbol, strategy))
                    elif fresh_proven_dual:
                        protected_proven_dual_book_ids.add(
                            (state_index, symbol, strategy)
                        )
                    elif price < entry and price < ma_value:
                        weak_book_ids.add((state_index, symbol, strategy))
        for pending_index, (signal, strategy) in enumerate(state.pending):
            if signal.direction == "buy":
                if (
                    evidence_symbols is not None
                    and signal.symbol not in evidence_symbols
                ):
                    excluded_buy_slots.append((state_index, pending_index))
                    continue
                shares = require_int("pending buy shares", signal.target_shares, min_value=0)
                price = require_finite("pending buy price", signal.price, min_value=0.000001)
                if signal.signal_date is None or pd.Timestamp(signal.signal_date) > date:
                    raise ValueError("account budget requires close-known buy intents")
                book_id = (state_index, signal.symbol, signal.strategy_name)
                rsi = indicator_at_signal(
                    signal.symbol, 'rsi', signal.signal_date,
                )
                held_for_symbol = live_strategies.get(signal.symbol, set())
                if (
                    signal.strategy_name == 'dual_ma'
                    and rsi is not None
                    and rsi <= EARLY_DUAL_TRANSITION_RSI_MAX
                    and signal.symbol in completed_proven_atr_symbols
                    and 'atr_channel' not in held_for_symbol
                ):
                    proven_early_dual_book_ids.add(book_id)
                buys.append((state_index, signal, shares*price))
                buy_slots.append((state_index, pending_index))
    shock_frames = {
        symbol: frame for state in states for symbol, frame in state.data_map.items()
    }
    receipt, planned_actions = plan_account_risk_budget(
        assets, peak, costs, books, buys, score, date_str=date_str,
        stress_by_symbol=observed_shock_stress(shock_frames, date, costs),
        direct_loss_by_symbol=observed_direct_losses(shock_frames, date, costs),
        shock_episode_active=bool(next((event.get('shock_episode_active', False)
            for event in reversed(events)
            if event.get('event') == 'account_budget_envelope'), False)),
        shock_floor=shock_floor,
        preserve_strategy_valid_holdings=preserve_strategy_valid_holdings,
        risk_alert_active=risk_alert_active,
        weak_book_ids=weak_book_ids,
        strategy_handoff_symbols=strategy_handoff_symbols,
        repeated_proven_reentry_symbols=repeated_proven_reentry_symbols,
        proven_early_dual_book_ids=proven_early_dual_book_ids,
        shock_reduced_book_ids=shock_reduced_book_ids,
        confirmed_shock_reduced_book_ids=confirmed_shock_reduced_book_ids,
        crowded_shock_reduced_book_ids=crowded_shock_reduced_book_ids,
    )
    receipt['protected_handoff_book_ids'] = sorted(protected_handoff_book_ids)
    receipt['protected_proven_dual_book_ids'] = sorted(
        protected_proven_dual_book_ids
    )
    receipt['portfolio_evidence_buy_symbols'] = (
        None if evidence_symbols is None else sorted(evidence_symbols)
    )
    receipt['portfolio_excluded_buy_slots'] = [
        list(slot) for slot in excluded_buy_slots
    ]
    cap = receipt["gross_cap"]
    buy_scales = receipt["buy_scales"]
    if len(buy_scales) != len(buy_slots):
        raise RuntimeError('account budget buy scales lost queue alignment')
    scale_by_slot = dict(zip(buy_slots, buy_scales, strict=True))
    actions = [action for action in planned_actions if not any(
        signal.direction == "sell" and signal.symbol == action.symbol
        and signal.strategy_name == action.strategy_name
        and signal.target_shares >= action.shares
        for signal, _ in states[action.state_index].pending
    )]
    # Validate and plan the entire batch before changing any pending queue.
    previous = [list(state.pending) for state in states]
    clipped = 0
    for state_index, state in enumerate(states):
        retained = []
        for pending_index, (signal, strategy) in enumerate(state.pending):
            buy_scale = scale_by_slot.get((state_index, pending_index), 1.)
            if signal.direction == "buy" and buy_scale < 1.:
                quantity = floor_to_lot(signal.target_shares * buy_scale)
                clipped += signal.target_shares - quantity
                state.sleeve._record_order_event(
                    date=date_str, signal=signal, event="account_budget_buy_reduced",
                    requested_shares=int(signal.target_shares),
                    authorized_shares=quantity, close_gross_cap=cap,
                )
                if not quantity:
                    continue
                signal = replace(signal, target_shares=quantity)
            retained.append((signal, strategy))
        state.pending = retained
    apply_risk_actions(actions, states, date_str=date_str, events=events,
                       state_local_books=True)
    for state, before in zip(states, previous):
        reconcile_close_queue(state.sleeve, before, state.pending, date_str, "account_budget_envelope")
    events.append({"date": date_str, "event": "account_budget_envelope",
                   "mechanism": "AB5", "planned_not_filled": True, **receipt,
                   "buy_shares_removed": clipped,
                   "new_reduction_orders": len(actions)})


def account_budget_status(events: Sequence[Mapping[str, Any]], enabled: bool) -> dict[str, Any]:
    """Report actual evaluation rather than claiming success from a config flag."""
    last = next((dict(e) for e in reversed(events) if e.get("event") == "account_budget_envelope"), None)
    return {"enabled": enabled, "mechanism": "AB5",
            "status": "APPLIED" if enabled and last is not None else
                      "NOT_EVALUATED" if enabled else "DISABLED_DIAGNOSTIC",
            "latest": last}
