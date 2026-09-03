"""Contracts for the one authoritative current 17-symbol universe."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from scripts.download_eastmoney_qfq import DEFAULT_SYMBOLS as DOWNLOAD_SYMBOLS
from quantfusion.application.backtest_cli import DEFAULT_SYMBOLS as BACKTEST_DEFAULTS
from quantfusion.application.backtest_cli import SYMBOL_NAME_TABLE
from quantfusion.config import daily, profiles
from quantfusion.config.overlay import SYMBOL_SUB_INDUSTRY
from quantfusion.config.paths import MARKET_DATA_DIR
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.universe import (
    ESTABLISHED_BASE_CORE,
    ESTABLISHED_EXPANSION_CORE,
    SYMBOL_NAMES,
    VALIDATION_UNIVERSES,
)
from quantfusion.engine.universe import BacktestEngine
from quantfusion.execution.priorities import EXECUTION_PRIORITY


EXPECTED_SYMBOLS = (
    "300308",
    "300502",
    "300394",
    "688256",
    "603986",
    "688072",
    "688300",
    "300054",
    "688361",
    "002409",
    "688498",
    "688120",
    "002384",
    "688082",
    "300604",
    "601869",
    "300408",
)
FIXED_SIGNAL_REFERENCE = (
    "300308",
    "300502",
    "300394",
    "688008",
    "603986",
)


def test_current_entry_points_share_one_ordered_universe() -> None:
    assert tuple(SYMBOL_NAMES) == EXPECTED_SYMBOLS
    assert tuple(daily.SYMBOLS) == EXPECTED_SYMBOLS
    assert daily.SYMBOLS == SYMBOL_NAMES
    assert tuple(EXECUTION_PRIORITY) == EXPECTED_SYMBOLS
    assert tuple(DOWNLOAD_SYMBOLS) == EXPECTED_SYMBOLS
    assert tuple(SYMBOL_NAME_TABLE) == EXPECTED_SYMBOLS
    assert tuple(BACKTEST_DEFAULTS) == EXPECTED_SYMBOLS[:5]


def test_validation_universes_are_prefixes_of_the_current_universe() -> None:
    assert VALIDATION_UNIVERSES == {
        "1_symbol": EXPECTED_SYMBOLS[:1],
        "3_symbols": EXPECTED_SYMBOLS[:3],
        "5_symbols": EXPECTED_SYMBOLS[:5],
        "13_symbols": EXPECTED_SYMBOLS[:13],
        "17_symbols": EXPECTED_SYMBOLS,
    }
    assert ESTABLISHED_BASE_CORE == frozenset(EXPECTED_SYMBOLS[:5])
    assert ESTABLISHED_EXPANSION_CORE == frozenset(EXPECTED_SYMBOLS[:13])


def test_signal_reference_is_loaded_beside_but_not_inside_the_trade_pool() -> None:
    policy = PortfolioPolicy()
    assert policy.regime_symbols == FIXED_SIGNAL_REFERENCE
    assert "688008" not in SYMBOL_NAMES
    states = [
        SimpleNamespace(
            data_map={code: object() for code in (*EXPECTED_SYMBOLS, *FIXED_SIGNAL_REFERENCE)}
        )
    ]
    assert BacktestEngine._reference_evidence_complete(
        states, policy.regime_symbols
    )


def test_every_current_symbol_has_strict_routing_and_risk_metadata() -> None:
    expected = set(EXPECTED_SYMBOLS)
    assert expected <= set(profiles.KNOWN_CLASSIFICATION)
    assert expected <= set(profiles.SYMBOL_GROUPS)
    assert expected <= set(profiles.SYMBOL_PROFILES)
    assert expected <= set(SYMBOL_SUB_INDUSTRY)


def test_every_current_symbol_has_verified_frozen_market_data() -> None:
    manifest = json.loads(
        (MARKET_DATA_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    checksum_entries = {}
    for line in (MARKET_DATA_DIR / "SHA256SUMS").read_text(
        encoding="utf-8"
    ).splitlines():
        digest, filename = line.split(maxsplit=1)
        checksum_entries[filename] = digest
    for symbol in EXPECTED_SYMBOLS:
        filename = f"{symbol}.csv"
        path = Path(MARKET_DATA_DIR) / filename
        assert path.is_file()
        assert symbol in manifest["symbols"]
        assert filename in checksum_entries
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum_entries[
            filename
        ]
