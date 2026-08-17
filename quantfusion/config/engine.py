"""Public engine defaults; compatibility classes delegate here."""

from __future__ import annotations

import math

from quantfusion.domain.rules import (
    SYMBOL_RE as _SYMBOL_RE,
    require_bool as _require_bool,
    require_finite as _require_finite,
    require_int as _require_int,
    require_positive as _require_positive,
)


def default_engine_config() -> dict:
    """Return the complete auditable strategy and execution defaults."""
    # Values are explicit to keep every historical run auditable. Industry
    # profiles below copy this dictionary and override only declared fields.
    return {
        "entry_period": 8,
        "exit_period": 3,
        "adx_threshold": 12,
        "adx_period": 10,
        "atr_period": 10,
        "rsi_period": 20,
        "ma_short": 15,
        "ma_long": 60,
        "atr_multiplier": 1.0,
        "trail_atr_mult": 4.0,
        "channel_mult": 2.0,
        "channel_lower_mult": 3.0,
        "risk_pct": 0.03,
        "hard_stop": 0.15,
        "strategy_weight": 0.98,
        "max_symbol_weight": 0.6,
        "max_total_weight": 1.0,
        "max_units": 20,
        "max_drawdown": 0.165,
        "daily_loss_limit": 0.06,
        "sector_guard_enabled": True,
        "sector_guard_min_symbols": 5,
        "sector_shock_return": -0.05,
        "sector_shock_breadth": 0.2,
        "sector_shock_ma": 5,
        "sector_shock_window": 4,
        "sector_shock_confirmations": 2,
        "sector_recovery_ma": 5,
        "sector_recovery_breadth": 0.8,
        "sector_recovery_confirmations": 2,
        # A sell is a symbol-level risk veto by default: it suppresses every
        # same-symbol buy, including another strategy's stale pending order.
        "symbol_level_sell_veto": True,
        "momentum_lookback": 5,
        "max_positions": 6,
        "group_min_slots": 2,
        "fusion_single_scale": 0.9,
        "fusion_double_scale": 1.0,
        "fusion_triple_scale": 1.1,
        "profit_lock_activation": 0.2,
        "profit_lock_giveback": 0.22,
        "reversal_break_giveback": 0.22,
        "reversal_exit_period": 6,
        "reversal_loss_cut": 0.1,
        "reversal_turtle_enabled": True,
        "reversal_dual_ma_enabled": True,
        "reversal_atr_channel_enabled": True,
        "combined_group_weight_limits": {
            "overseas_compute": 1.0,
            "domestic_semiconductor": 0.8,
        },
        "liquidate_on_circuit_breaker": True,
        "strict_unmapped": True,  # fail-closed: unmapped symbols raise instead of silently using default
        "commission_rate": 0.00025,
        "stamp_duty": 0.0005,
        "slippage": 0.001,
        "min_commission": 5.0,
        "max_pending_buy_days": 5,
        "pyramid_add_atr": 1.0,
        "pyramid_risk_decay": 1.0,
        "atr_method": "wilder",
        "limit_price_epsilon": 0.001,
        "per_symbol_limit_pct": {},
        "st_symbols": set(),
        "risk_free_rate": 0.0,
        # Market regime recognition: a fixed-basket state machine that gates
        # new entries (CHOPPY) and scales sizes (TRANSITION) before signals.
        # Enabled by default with conservative parameters: the state machine
        # only intervenes after multi-day confirmation of a genuine regime
        # shift, so TREND periods are fully open (zero-cost).  The volatility
        # fast-path was removed because vol_pct=1.0 during V-shaped corrections
        # blocked trend entries and reduced returns.
        "market_regime_enabled": True,
        "regime_ewi_lookback": 20,
        "regime_breadth_ma_long": 20,
        "regime_adx_trend": 25,
        "regime_adx_choppy": 20,
        "regime_hurst_window": 100,
        "regime_hurst_trend": 0.55,
        "regime_hurst_choppy": 0.45,
        "regime_vol_lookback": 60,
        "regime_vol_extreme_pct": 0.9,
        "regime_ewi_slope_trend": 0.02,
        "regime_ewi_slope_choppy": -0.02,
        "regime_score_trend": 2,
        "regime_score_choppy": -3,
        "regime_choppy_confirmations": 2,
        "regime_trend_confirmations": 3,
        "regime_recovery_confirmations": 3,
        "regime_min_state_hold": 3,
        "regime_transition_scale": 1.0,
        "regime_transition_pyramid_scale": 1.0,
        "regime_trend_to_transition_confirmations": 3,
        "regime_choppy_exit_ratio": 0.3,
        "regime_transition_exit_ratio": 0.0,
        "regime_transition_trim_confirmations": 5,
        # 穿越牛熊 (cross-market-cycle) overlay: a bull-silent defensive layer
        # on top of the ensemble. Default ON; only fires on genuine risk so a
        # clean bull run is untouched. shock_trim is opt-in (the ensemble
        # already carries regime de-risking + drawdown circuit breakers).
        "enable_cm_overlay": True,
        "cm_overlay_shock_trim": False,
        "cm_independent_risk_basket": True,
        "cm_trend_health_protection": True,
        "cm_risk_continuous_confirm_days": 3,
        "cm_risk_level2_drawdown": 0.08,
        "cm_risk_level3_drawdown": 0.12,
        "cm_risk_severe_direct_return": -0.10,
        # Keep the three independent sleeves, but reallocate only their
        # unused cash after a confirmed regime change. No position or
        # strategy ledger is merged, and TREND retains equal one-third
        # funding so bull-market behavior stays unchanged.
        "dynamic_sleeve_weights": True,
        "transition_fast_weight": 0.20,
        "transition_base_weight": 0.35,
        "transition_slow_weight": 0.45,
        "choppy_fast_weight": 0.10,
        "choppy_base_weight": 0.35,
        "choppy_slow_weight": 0.55,
        "adaptive_max_positions": True,
        "transition_max_positions": 4,
        "choppy_max_positions": 3,
        # Report P1-3: sticky candidates for large pools. Default ON: a
        # held symbol is retained until it stops qualifying, and a new name
        # only replaces one when it clearly beats the weakest held symbol,
        # reducing daily-rank churn (fee/slippage + selling winners). Set to
        # False to return to the pure daily cross-sectional top-N.
        "sticky_candidates": True,
        "adaptive_sticky_candidates": True,
        "sticky_min_score_gap": 0.15,
        "sticky_confirm_days": 4,
        "sticky_cycle_days": 5,
        "sticky_rotated_cooldown_days": 20,
        # Concentrated merged accounts remain in cash for one trading year
        # after an account-level tail lock. Sleeves keep their own policies.
        "concentrated_account_rearm_days": 252,
        # Candidate pools with five or more names but without the complete
        # fixed reference basket retain a cash reserve before any drawdown
        # signal can react to an overnight gap.
        "incomplete_reference_max_total_weight": 0.85,
        "established_expansion_min_score": 0.80,
        # Report P1-2: hierarchical sub-industry parameter shrinkage. Default
        # ON: each fine sub-industry profile (optical module, chip design,
        # equipment, test, material, packaging, ...) is pulled part-way back
        # toward its coarse parent for the report's "allowable" parameters
        # (max single-symbol weight, ATR multiple, risk budget), so a thin
        # sub-industry sample cannot over-fit a single stock or a single bull
        # run. 0.0 converges fully to the coarse parent, 1.0 keeps the fine
        # override unchanged. Default 0.5. Entry/exit periods, profit
        # protection, pyramid add-on and regime parameters are shared through
        # the hierarchy and are never shrunk.
        "subindustry_shrinkage": 0.5,
    }


PER_SYMBOL_OVERRIDE_KEYS: frozenset[str] = frozenset({
    "entry_period",
    "exit_period",
    "adx_threshold",
    "adx_period",
    "atr_period",
    "rsi_period",
    "ma_short",
    "ma_long",
    "atr_multiplier",
    "trail_atr_mult",
    "channel_mult",
    "channel_lower_mult",
    "risk_pct",
    "hard_stop",
    "strategy_weight",
    "max_symbol_weight",
    "max_units",
    "pyramid_add_atr",
    "pyramid_risk_decay",
    "atr_method",
    "profit_lock_activation",
    "profit_lock_giveback",
    "reversal_break_giveback",
    "reversal_exit_period",
    "reversal_loss_cut",
    "reversal_turtle_enabled",
    "reversal_dual_ma_enabled",
    "reversal_atr_channel_enabled",
})


def validate_engine_config(cfg: dict) -> dict:
    """Validate one complete engine configuration and normalize containers."""
    out = dict(cfg)
    allowed_keys = set(default_engine_config().keys())
    unknown_keys = sorted(set(out) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"Configuration contains unknown fields; check for typos: {unknown_keys}"
        )
    _validate_integer_config(out)
    _validate_numeric_config(out)
    _validate_boolean_config(out)
    _validate_container_config(out)
    if out["entry_period"] <= out["exit_period"]:
        raise ValueError("entry_period must be greater than exit_period")
    if out["ma_short"] >= out["ma_long"]:
        raise ValueError("ma_short must be less than ma_long")
    if out["sector_shock_confirmations"] > out["sector_shock_window"]:
        raise ValueError(
            "sector_shock_confirmations must not exceed sector_shock_window"
        )
    if out["max_symbol_weight"] > out["max_total_weight"]:
        raise ValueError("max_symbol_weight must not exceed max_total_weight")
    if out["strategy_weight"] > out["max_total_weight"]:
        raise ValueError("strategy_weight must not exceed max_total_weight")
    return out


def _validate_integer_config(out: dict) -> None:
    """Validate integer periods, counters, slots, and confirmation windows."""
    minimums = {
        "entry_period": 2,
        "exit_period": 1,
        "adx_period": 1,
        "atr_period": 1,
        "rsi_period": 1,
        "ma_short": 1,
        "ma_long": 2,
        "max_units": 1,
        "momentum_lookback": 1,
        "max_positions": 1,
        "max_pending_buy_days": 1,
        "group_min_slots": 0,
        "reversal_exit_period": 2,
        "sector_shock_ma": 2,
        "sector_shock_window": 2,
        "sector_shock_confirmations": 1,
        "sector_recovery_ma": 2,
        "sector_recovery_confirmations": 1,
        "sector_guard_min_symbols": 1,
        "regime_ewi_lookback": 2,
        "regime_breadth_ma_long": 1,
        "regime_hurst_window": 10,
        "regime_vol_lookback": 2,
        "regime_score_trend": -10,
        "regime_score_choppy": -10,
        "regime_choppy_confirmations": 1,
        "regime_trend_confirmations": 1,
        "regime_recovery_confirmations": 1,
        "regime_min_state_hold": 1,
        "regime_transition_trim_confirmations": 1,
        "cm_risk_continuous_confirm_days": 2,
        "transition_max_positions": 1,
        "choppy_max_positions": 1,
        "sticky_confirm_days": 1,
        "sticky_cycle_days": 1,
        "sticky_rotated_cooldown_days": 1,
        "concentrated_account_rearm_days": 1,
    }
    for key, minimum in minimums.items():
        out[key] = _require_int(key, out.get(key), min_value=minimum)


def _validate_numeric_config(out: dict) -> None:
    """Validate scalar thresholds, weights, costs, and strategy scales."""
    out["adx_threshold"] = _require_finite(
        "adx_threshold", out.get("adx_threshold"), min_value=0.0
    )
    out["pyramid_risk_decay"] = _require_finite(
        "pyramid_risk_decay",
        out.get("pyramid_risk_decay", 1.0),
        min_value=0.01,
        max_value=1.0,
    )
    out["limit_price_epsilon"] = _require_finite(
        "limit_price_epsilon",
        out.get("limit_price_epsilon", 0.001),
        min_value=0.0,
        max_value=0.1,
    )
    out["sector_shock_return"] = _require_finite(
        "sector_shock_return",
        out.get("sector_shock_return"),
        min_value=-1.0,
        max_value=0.0,
    )
    out["sector_shock_breadth"] = _require_finite(
        "sector_shock_breadth",
        out.get("sector_shock_breadth"),
        min_value=0.0,
        max_value=1.0,
    )
    out["sector_recovery_breadth"] = _require_finite(
        "sector_recovery_breadth",
        out.get("sector_recovery_breadth"),
        min_value=0.0,
        max_value=1.0,
    )
    atr_method = str(out.get("atr_method", "wilder")).lower()
    if atr_method not in {"wilder", "sma"}:
        raise ValueError(
            f"atr_method must be 'wilder' or 'sma', got {atr_method!r}"
        )
    out["atr_method"] = atr_method
    for key in [
        "atr_multiplier",
        "trail_atr_mult",
        "channel_mult",
        "channel_lower_mult",
        "pyramid_add_atr",
    ]:
        out[key] = _require_positive(key, out.get(key))
    for key in [
        "risk_pct",
        "hard_stop",
        "strategy_weight",
        "max_symbol_weight",
        "max_drawdown",
        "daily_loss_limit",
        "profit_lock_activation",
        "profit_lock_giveback",
        "reversal_break_giveback",
        "reversal_loss_cut",
    ]:
        out[key] = _require_positive(
            key, out.get(key), max_value=1.0, inclusive_max=False
        )
    out["max_total_weight"] = _require_positive(
        "max_total_weight",
        out.get("max_total_weight"),
        max_value=1.0,
        inclusive_max=True,
    )
    out["incomplete_reference_max_total_weight"] = _require_positive(
        "incomplete_reference_max_total_weight",
        out.get("incomplete_reference_max_total_weight"),
        max_value=1.0,
        inclusive_max=True,
    )
    out["established_expansion_min_score"] = _require_finite(
        "established_expansion_min_score",
        out.get("established_expansion_min_score"),
        min_value=0.0,
        max_value=1.0,
    )
    for key in ["commission_rate", "stamp_duty", "slippage"]:
        out[key] = _require_finite(
            key, out.get(key), min_value=0.0, max_value=1.0, inclusive_max=False
        )
    out["min_commission"] = _require_finite(
        "min_commission", out.get("min_commission", 0.0), min_value=0.0
    )
    out["risk_free_rate"] = _require_finite(
        "risk_free_rate",
        out.get("risk_free_rate", 0.0),
        min_value=-0.99,
        max_value=1.0,
    )
    # Report P1-2: sub-industry shrinkage factor must stay in [0, 1].
    out["subindustry_shrinkage"] = _require_finite(
        "subindustry_shrinkage",
        out.get("subindustry_shrinkage", 0.5),
        min_value=0.0,
        max_value=1.0,
    )
    for key in [
        "fusion_single_scale",
        "fusion_double_scale",
        "fusion_triple_scale",
    ]:
        out[key] = _require_positive(
            key, out.get(key), max_value=2.0, inclusive_max=True
        )
    # Market regime numeric thresholds: trend thresholds must sit above
    # their choppy counterparts so the scoring votes are well ordered.
    out["regime_adx_trend"] = _require_finite(
        "regime_adx_trend", out.get("regime_adx_trend"), min_value=0.0
    )
    out["regime_adx_choppy"] = _require_finite(
        "regime_adx_choppy", out.get("regime_adx_choppy"), min_value=0.0
    )
    out["regime_hurst_trend"] = _require_finite(
        "regime_hurst_trend",
        out.get("regime_hurst_trend"),
        min_value=0.0,
        max_value=1.0,
    )
    out["regime_hurst_choppy"] = _require_finite(
        "regime_hurst_choppy",
        out.get("regime_hurst_choppy"),
        min_value=0.0,
        max_value=1.0,
    )
    out["regime_vol_extreme_pct"] = _require_positive(
        "regime_vol_extreme_pct",
        out.get("regime_vol_extreme_pct"),
        max_value=1.0,
        inclusive_max=True,
    )
    out["regime_ewi_slope_trend"] = _require_finite(
        "regime_ewi_slope_trend",
        out.get("regime_ewi_slope_trend"),
        min_value=-1.0,
        max_value=1.0,
    )
    out["regime_ewi_slope_choppy"] = _require_finite(
        "regime_ewi_slope_choppy",
        out.get("regime_ewi_slope_choppy"),
        min_value=-1.0,
        max_value=1.0,
    )
    out["regime_transition_scale"] = _require_finite(
        "regime_transition_scale",
        out.get("regime_transition_scale"),
        min_value=0.0,
        max_value=1.0,
    )
    out["regime_transition_pyramid_scale"] = _require_finite(
        "regime_transition_pyramid_scale",
        out.get("regime_transition_pyramid_scale"),
        min_value=0.0,
        max_value=1.0,
    )
    for key in (
        "cm_risk_level2_drawdown",
        "cm_risk_level3_drawdown",
    ):
        out[key] = _require_positive(
            key, out.get(key), max_value=1.0, inclusive_max=False
        )
    if out["cm_risk_level3_drawdown"] <= out["cm_risk_level2_drawdown"]:
        raise ValueError(
            "cm_risk_level3_drawdown must exceed cm_risk_level2_drawdown"
        )
    out["cm_risk_severe_direct_return"] = _require_finite(
        "cm_risk_severe_direct_return",
        out.get("cm_risk_severe_direct_return"),
        min_value=-1.0,
        max_value=0.0,
    )
    for key in (
        "transition_fast_weight",
        "transition_base_weight",
        "transition_slow_weight",
        "choppy_fast_weight",
        "choppy_base_weight",
        "choppy_slow_weight",
    ):
        out[key] = _require_finite(
            key, out.get(key), min_value=0.0, max_value=1.0
        )
    for prefix in ("transition", "choppy"):
        total = sum(
            out[f"{prefix}_{sleeve}_weight"]
            for sleeve in ("fast", "base", "slow")
        )
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(f"{prefix} sleeve weights must sum to 1.0")
    out["sticky_min_score_gap"] = _require_finite(
        "sticky_min_score_gap",
        out.get("sticky_min_score_gap"),
        min_value=0.0,
        max_value=1.0,
    )
    if out["regime_adx_trend"] <= out["regime_adx_choppy"]:
        raise ValueError("regime_adx_trend must be greater than regime_adx_choppy")
    if out["regime_hurst_trend"] <= out["regime_hurst_choppy"]:
        raise ValueError(
            "regime_hurst_trend must be greater than regime_hurst_choppy"
        )
    if out["regime_ewi_slope_trend"] <= out["regime_ewi_slope_choppy"]:
        raise ValueError(
            "regime_ewi_slope_trend must be greater than regime_ewi_slope_choppy"
        )


def _validate_boolean_config(out: dict) -> None:
    """Reject truthy strings and integers for every Boolean option."""
    boolean_keys = (
        "liquidate_on_circuit_breaker",
        "sector_guard_enabled",
        "strict_unmapped",
        "symbol_level_sell_veto",
        "reversal_turtle_enabled",
        "reversal_dual_ma_enabled",
        "reversal_atr_channel_enabled",
        "market_regime_enabled",
        "enable_cm_overlay",
        "cm_overlay_shock_trim",
        "cm_independent_risk_basket",
        "cm_trend_health_protection",
        "dynamic_sleeve_weights",
        "adaptive_max_positions",
        "sticky_candidates",
        "adaptive_sticky_candidates",
    )
    for key in boolean_keys:
        out[key] = _require_bool(key, out.get(key))


def _validate_container_config(out: dict) -> None:
    """Normalize sector caps, symbol limit overrides, and ST symbol codes."""
    group_limits = out.get("combined_group_weight_limits", {})
    if not isinstance(group_limits, dict):
        raise ValueError("combined_group_weight_limits must be a dict")
    allowed_groups = {"overseas_compute", "domestic_semiconductor"}
    unknown_groups = sorted(set(map(str, group_limits)) - allowed_groups)
    if unknown_groups:
        raise ValueError(
            f"combined_group_weight_limits contains unknown sector pools: {unknown_groups}"
        )
    out["combined_group_weight_limits"] = {
        str(group): _require_positive(
            f"combined_group_weight_limits[{group}]",
            value,
            max_value=1.0,
            inclusive_max=True,
        )
        for group, value in group_limits.items()
    }
    per_symbol_limit_pct = out.get("per_symbol_limit_pct", {}) or {}
    if not isinstance(per_symbol_limit_pct, dict):
        raise ValueError("per_symbol_limit_pct must be a dict")
    normalized_limit_overrides: dict[str, float] = {}
    for code, pct in per_symbol_limit_pct.items():
        code_str = str(code)
        if not _SYMBOL_RE.match(code_str):
            raise ValueError(
                f"per_symbol_limit_pct contains an invalid stock code: {code!r}"
            )
        normalized_limit_overrides[code_str] = _require_positive(
            f"per_symbol_limit_pct[{code_str}]",
            pct,
            max_value=1.0,
            inclusive_max=False,
        )
    out["per_symbol_limit_pct"] = normalized_limit_overrides
    st_symbols = out.get("st_symbols", set()) or set()
    if isinstance(st_symbols, str) or not isinstance(
        st_symbols, (set, list, tuple)
    ):
        raise ValueError("st_symbols must be a collection of stock codes")
    normalized_st_symbols = {str(code) for code in st_symbols}
    bad_st = [code for code in normalized_st_symbols if not _SYMBOL_RE.match(code)]
    if bad_st:
        raise ValueError(f"st_symbols contains an invalid stock code: {bad_st}")
    out["st_symbols"] = normalized_st_symbols
