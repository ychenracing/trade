"""Compatibility facade for canonical real-account decision support."""

# ruff: noqa: F401

from quantfusion.account.models import (
    AccountPosition,
    AccountSnapshot,
    AccountTarget,
    PointInTimeSignal,
)
from quantfusion.account.service import (
    compute_target_shares,
    target_weight_for,
    trend_candidate_score,
)
from quantfusion.account.snapshot import load_account_snapshot
from quantfusion.application.account_scan import AccountSignalEngine, run_account_scan
from quantfusion.data import contracts as market_data_contracts
from quantfusion.io.artifacts import atomic_json

_trend_candidate_score = trend_candidate_score
_target_weight_for = target_weight_for
_compute_target_shares = compute_target_shares
_atomic_json = atomic_json
