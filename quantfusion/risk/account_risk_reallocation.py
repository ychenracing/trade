"""Opportunity-driven minimum account-risk capacity reallocation."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from quantfusion.domain.models import Signal
from quantfusion.risk.account_risk_capacity import (
    _finite_score,
    _group_debit,
)
from quantfusion.risk.overlay.models import RiskAction

_CAPACITY_REALLOCATION_PRIORITY = 70
_EPSILON = 1e-8


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    sellable_shares: Mapping[tuple[int, str, str], int]
    queued_sell_books: frozenset[tuple[int, str, str]]
    rearm_consumption_ready: bool
    rearm_pending_validation: bool


def _fits_reallocation_target(
    books: Sequence[tuple[int, str, str, int, float]],
    buy_values: Sequence[tuple[str, float]],
    sold: Mapping[tuple[int, str, str], int],
    cfg: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    gross = sum(
        max(0, shares - int(sold.get((state, symbol, strategy), 0))) * price
        for state, symbol, strategy, shares, price in books
    ) + sum(value for _, value in buy_values)
    if gross > float(receipt["gross_cap"]) + _EPSILON:
        return False
    debit = _group_debit(
        books,
        buy_values,
        cfg,
        cost_rate=float(receipt["cost_rate"]),
        stress_fraction=float(receipt["stress_fraction"]),
        sold_shares=sold,
    )
    return debit <= float(receipt["remaining_loss_budget"]) + _EPSILON


def _plan_capacity_reallocation(
    books: Sequence[tuple[int, str, str, int, float]],
    buys: Sequence[tuple[int, Signal, float]],
    buy_scales: Sequence[float],
    score: Callable[[str], float],
    cfg: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    date_str: str,
    execution: ExecutionContext | None,
) -> tuple[list[RiskAction], list[dict[str, Any]]]:
    if execution is None or not execution.rearm_consumption_ready:
        return [], []
    unmet_total = sum(
        max(0.0, value * (1.0 - scale))
        for (_, _, value), scale in zip(buys, buy_scales, strict=True)
    )
    if unmet_total <= _EPSILON:
        return [], []
    price_by_book = {
        (state, symbol, strategy): price
        for state, symbol, strategy, _, price in books
    }
    shares_by_book = {
        (state, symbol, strategy): shares
        for state, symbol, strategy, shares, _ in books
    }
    planned: dict[tuple[int, str, str], int] = {}
    restored_values: list[tuple[str, float]] = [
        (signal.symbol, value * scale)
        for (_, signal, value), scale in zip(buys, buy_scales, strict=True)
        if value * scale > _EPSILON
    ]
    plans: list[dict[str, Any]] = []
    outstanding_total = unmet_total
    buy_order = sorted(
        range(len(buys)),
        key=lambda index: (
            -_finite_score(score, buys[index][1].symbol),
            buys[index][1].symbol,
            buys[index][0],
            buys[index][1].strategy_name,
        ),
    )
    for buy_index in buy_order:
        target_state, signal, requested = buys[buy_index]
        approved = requested * buy_scales[buy_index]
        missing = max(0.0, requested - approved)
        if missing <= _EPSILON:
            continue
        target_score = _finite_score(score, signal.symbol)
        candidates = sorted(
            (
                (state, symbol, strategy)
                for state, symbol, strategy, shares, _ in books
                if shares > 0
                and (state, symbol, strategy)
                not in execution.queued_sell_books
                and symbol != signal.symbol
                and _finite_score(score, symbol) < target_score
                and int(execution.sellable_shares.get(
                    (state, symbol, strategy), 0
                )) - planned.get((state, symbol, strategy), 0) >= 100
            ),
            key=lambda book: (
                _finite_score(score, book[1]),
                book[1],
                book[0],
                book[2],
            ),
        )
        if not candidates:
            continue
        before = dict(planned)
        target_buys = [*restored_values, (signal.symbol, missing)]
        while not _fits_reallocation_target(
            books, target_buys, planned, cfg, receipt
        ):
            source = next(
                (
                    book
                    for book in candidates
                    if int(execution.sellable_shares.get(book, 0))
                    - planned.get(book, 0)
                    >= 100
                ),
                None,
            )
            if source is None:
                planned = before
                break
            lot_value = 100 * price_by_book[source]
            if lot_value > outstanding_total + _EPSILON and planned == before:
                planned = before
                break
            planned[source] = planned.get(source, 0) + 100
            if planned[source] > shares_by_book[source]:
                planned = before
                break
        else:
            restored_values.append((signal.symbol, missing))
            outstanding_total = max(0.0, outstanding_total - missing)
            deficit_payload = {
                "date": date_str,
                "target_state": target_state,
                "target_symbol": signal.symbol,
                "target_strategy": signal.strategy_name,
                "requested_value": round(requested, 8),
                "approved_value": round(approved, 8),
                "gross_cap": round(float(receipt["gross_cap"]), 8),
            }
            identity = hashlib.sha256(
                json.dumps(
                    deficit_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            sources = []
            for source, shares in sorted(
                (
                    (book, planned.get(book, 0) - before.get(book, 0))
                    for book in planned
                ),
                key=lambda item: (
                    _finite_score(score, item[0][1]),
                    item[0][1],
                    item[0][0],
                    item[0][2],
                ),
            ):
                if shares <= 0:
                    continue
                sources.append(
                    {
                        "state_index": source[0],
                        "symbol": source[1],
                        "strategy": source[2],
                        "shares": shares,
                    }
                )
            plans.append(
                {
                    **deficit_payload,
                    "deficit_identity": identity,
                    "source_account_state": [
                        {
                            "state_index": state,
                            "symbol": symbol,
                            "strategy": strategy,
                            "held_shares": shares_by_book[(state, symbol, strategy)],
                            "sellable_shares": int(
                                execution.sellable_shares.get(
                                    (state, symbol, strategy), 0
                                )
                            ),
                        }
                        for state, symbol, strategy in candidates
                    ],
                    "release_sources": sources,
                }
            )
    actions = []
    identity_by_source: dict[tuple[int, str, str], str] = {}
    for plan in plans:
        for source in plan["release_sources"]:
            identity_by_source[
                (
                    int(source["state_index"]),
                    str(source["symbol"]),
                    str(source["strategy"]),
                )
            ] = str(plan["deficit_identity"])
    for book, shares in sorted(
        planned.items(), key=lambda item: (item[0][1], item[0][0], item[0][2])
    ):
        if shares <= 0:
            continue
        state, symbol, strategy = book
        actions.append(
            RiskAction(
                symbol=symbol,
                strategy_name=strategy,
                shares=shares,
                price=price_by_book[book],
                signal_date=date_str,
                reason="capacity_reallocation",
                priority=_CAPACITY_REALLOCATION_PRIORITY,
                extra=f"deficit={identity_by_source.get(book, '')[:16]}",
                state_index=state,
            )
        )
    return actions, plans
