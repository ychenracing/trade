"""Daily pending-signal classification and buy suppression."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from quantfusion.application.daily_support import classify_signal

_classify_signal = classify_signal

def _apply_buy_suppression(
    sigs: list[Any], suppress_buys: bool
) -> tuple[str, str, bool]:
    """Apply buy suppression to a list of pending signals for one symbol.

    Returns ``(signal_label, strategies, was_suppressed)``.

    When ``suppress_buys`` is ``True``:
    - Pure buy signals are replaced with "观望 (风险状态不匹配)".
    - Mixed buy/sell signals keep only the sell signals and append
      "[买入已抑制]" to the label.
    - Pure sell (or non-buy) signals are shown unchanged.

    Only ``sell`` directions are retained in mixed signals — other
    non-buy directions (e.g. ``hold``) are excluded to avoid mislabeling.
    """
    if not sigs:
        return ("", "", False)

    directions = sorted({s.direction for s in sigs})
    if len(directions) == 1:
        signal_label = _classify_signal(sigs[0])
        strategies = ", ".join(sorted({s.strategy_name for s in sigs}))
    else:
        parts = []
        for d in directions:
            d_sigs = [s for s in sigs if s.direction == d]
            parts.append(f"{_classify_signal(d_sigs[0])}({len(d_sigs)})")
        signal_label = " + ".join(parts)
        strategies = ", ".join(sorted({s.strategy_name for s in sigs}))

    was_suppressed = False
    if suppress_buys:
        buy_sigs = [s for s in sigs if s.direction == "buy"]
        sell_sigs = [s for s in sigs if s.direction == "sell"]
        if buy_sigs and sell_sigs:
            # Mixed buy/sell: keep only sell, suppress buy
            sell_dirs = sorted({s.direction for s in sell_sigs})
            parts = []
            for d in sell_dirs:
                d_sigs = [s for s in sell_sigs if s.direction == d]
                parts.append(f"{_classify_signal(d_sigs[0])}({len(d_sigs)})")
            signal_label = " + ".join(parts) + " [买入已抑制]"
            strategies = ", ".join(
                sorted({s.strategy_name for s in sell_sigs})
            )
            was_suppressed = True
        elif buy_sigs:
            # Pure buy (or buy + non-sell): suppress entirely
            signal_label = "观望 (风险状态不匹配)"
            strategies = "—"
            was_suppressed = True

    return (signal_label, strategies, was_suppressed)


def _serialize_pending_signals(
    pending: list[Any], suppress_buys: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Serialize pending signals into executable and blocked lists.

    Returns ``(executable_signals, blocked_signals)``.

    Blocked buy signals are moved to a separate ``blocked_signals`` list so
    that ``pending_signals`` only contains executable signals. This is the
    true fail-closed approach: downstream consumers that only check
    ``direction == "buy"`` on ``pending_signals`` will never see blocked
    buys, without needing to check the ``executable`` flag.

    Each entry in both lists includes ``blocked`` and ``executable`` flags
    for explicit machine consumption.
    """
    executable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for sig in pending:
        try:
            entry = asdict(sig)
        except TypeError:
            entry = {
                "symbol": getattr(sig, "symbol", ""),
                "direction": getattr(sig, "direction", ""),
                "strategy_name": getattr(sig, "strategy_name", ""),
                "target_shares": getattr(sig, "target_shares", 0),
                "price": getattr(sig, "price", 0.0),
                "reason": getattr(sig, "reason", ""),
                "signal_date": getattr(sig, "signal_date", ""),
            }
        if suppress_buys and entry.get("direction") == "buy":
            entry["blocked"] = True
            entry["blocked_reason"] = "risk_state_identity_mismatch"
            entry["executable"] = False
            blocked.append(entry)
        else:
            entry["blocked"] = False
            entry["executable"] = True
            executable.append(entry)
    return executable, blocked


apply_buy_suppression = _apply_buy_suppression
serialize_pending_signals = _serialize_pending_signals
