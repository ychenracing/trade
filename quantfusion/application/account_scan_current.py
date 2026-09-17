"""Current real-account decision-support surface with AB5 observation-only policy."""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pandas as pd

from quantfusion.account.models import AccountSnapshot
from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.domain.models import Signal
from quantfusion.domain.rules import require_finite
from quantfusion.engine.universe import SleeveBacktestEngine
from quantfusion.risk.account_budget import (
    observed_direct_losses,
    observed_shock_stress,
    plan_account_risk_budget,
)

legacy = importlib.import_module("quantfusion.application.account_scan")
_PreparedMarket = legacy._PreparedMarket


class AccountSignalEngine(legacy.AccountSignalEngine):
    """Real-account advice with AB5 measured but excluded from trade decisions."""

    @staticmethod
    def _apply_account_budget(
        snapshot: AccountSnapshot,
        prepared: dict[str, _PreparedMarket],
        actions: list[dict[str, Any]],
        *,
        equity: float,
        as_of: str,
    ) -> dict[str, Any]:
        cfg = default_engine_config()
        for _, _, symbol_cfg, _ in prepared.values():
            for key in ("slippage", "commission_rate", "stamp_duty", "min_commission"):
                cfg[key] = max(cfg[key], symbol_cfg.get(key, cfg[key]))
        peak = max(
            require_finite("peak_equity", snapshot.peak_equity, min_value=0.01),
            equity,
        )
        by_symbol = {
            row["symbol"]: row for row in actions if row.get("shares", 0) > 0
        }
        books = [
            (
                0,
                position.symbol,
                "account_position",
                position.shares,
                by_symbol[position.symbol]["close"],
            )
            for position in snapshot.positions
        ]
        buy_rows = [row for row in actions if row["action"] == "BUY_CANDIDATE"]
        buys = [
            (
                0,
                Signal(
                    row["symbol"],
                    "account_candidate",
                    "buy",
                    target_shares=row["indicative_target_shares"],
                    price=row["close"],
                    signal_date=as_of,
                ),
                row["indicative_target_shares"] * row["close"],
            )
            for row in buy_rows
        ]
        frames = {symbol: market[0] for symbol, market in prepared.items()}
        scores = [
            SleeveBacktestEngine(
                peak,
                cfg=cfg,
                policy=PortfolioPolicy(),
                allocation_lookbacks=lookbacks,
                sleeve_name="account_risk_score",
            )._allocation_scores(frames, pd.Timestamp(as_of))
            for lookbacks in PortfolioPolicy().allocation_horizons
        ]

        def score(symbol: str) -> float:
            return sum(values.get(symbol, 0.0) for values in scores) / len(scores)

        evidence_date = pd.Timestamp(as_of)
        receipt, _counterfactual_reductions = plan_account_risk_budget(
            equity,
            peak,
            cfg,
            books,
            buys,
            score,
            date_str=as_of,
            stress_by_symbol=observed_shock_stress(frames, evidence_date, cfg),
            direct_loss_by_symbol=observed_direct_losses(frames, evidence_date, cfg),
        )
        observed_buy_scales = list(receipt.pop("buy_scales", []))
        observed_buy_scale = float(receipt.pop("buy_scale", 1.0))
        if len(observed_buy_scales) != len(buys):
            raise RuntimeError("account budget observation lost buy alignment")

        return {
            "enabled": True,
            "mechanism": "AB5",
            "policy_mode": "OBSERVE_ONLY",
            "health_status": "EVALUATED",
            "status": "OBSERVED",
            "trade_intervention_allowed": False,
            "scope": "actual_account_snapshot_books",
            "input_peak_equity": snapshot.peak_equity,
            **receipt,
            "buy_scale": 1.0,
            "buy_scales": [1.0] * len(buys),
            "observed_counterfactual_buy_scale": observed_buy_scale,
            "observed_counterfactual_buy_scales": observed_buy_scales,
            "buy_shares_removed": 0,
            "new_reduction_orders": 0,
        }

    def run(
        self,
        snapshot: AccountSnapshot,
        symbols: dict[str, str],
        *,
        as_of: str,
        expected_account_id: str = "main",
    ) -> dict[str, Any]:
        result = super().run(
            snapshot,
            symbols,
            as_of=as_of,
            expected_account_id=expected_account_id,
        )
        budget = result.get("account_risk_budget")
        if isinstance(budget, dict) and budget.get("status") == "NOT_READY":
            budget.update(
                policy_mode="OBSERVE_ONLY",
                health_status="NOT_EVALUATED",
                status="NOT_EVALUATED",
                trade_intervention_allowed=False,
            )
        return result


def run_account_scan(
    *,
    account_path: str,
    symbols: dict[str, str],
    end_date: str,
    cache_dir: str,
    regime_data_dir: str,
    output_dir: str,
    expected_account_id: str = "main",
    calendar_file: str | Path = legacy.DEFAULT_CALENDAR_FILE,
) -> int:
    """Run the current real-account scan with observation-only AB5 semantics."""
    try:
        snapshot, snapshot_sha256 = legacy.load_account_snapshot_with_sha256(
            account_path
        )
        result = AccountSignalEngine(
            cache_dir=cache_dir,
            regime_data_dir=regime_data_dir,
            calendar_file=calendar_file,
        ).run(
            snapshot,
            symbols,
            as_of=end_date,
            expected_account_id=expected_account_id,
        )
        result["account_snapshot_sha256"] = snapshot_sha256
        output = Path(output_dir) / f"account_signals_{end_date}.json"
        legacy.atomic_json(result, output)
    except legacy.EXPECTED_DATA_ERRORS as exc:
        print(f"Account signal scan failed: {exc}")
        return 1

    legacy.publish_daily_report(
        output,
        symbols=symbols,
        expected_identity=("account_snapshot_sha256", snapshot_sha256),
    )
    print(f"机器结果已保存：{output}")
    return 0


__all__ = ["AccountSignalEngine", "run_account_scan"]
