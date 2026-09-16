"""Research-universe contracts that must not alter the production 17-symbol pool."""

from __future__ import annotations

import pandas as pd

from scripts import download_eastmoney_qfq as download
from quantfusion.application.backtest_cli import build_argument_parser
from quantfusion.config import profiles
from quantfusion.config.overlay import RISK_BASKET, SYMBOL_SUB_INDUSTRY
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.research_universes import (
    DEFAULT_RESEARCH_START_DATE,
    RESEARCH_SYMBOL_NAMES,
    UNIVERSE_POOLS,
    symbols_for_pool,
)
from quantfusion.config.universe import SYMBOL_NAMES


EXPECTED_NAMES = {
    "pool_a": ("中际旭创",),
    "pool_b": ("中际旭创", "新易盛", "天孚通信"),
    "pool_c": ("中际旭创", "新易盛", "天孚通信", "澜起科技", "兆易创新"),
    "pool_d": (
        "中际旭创", "新易盛", "天孚通信", "澜起科技", "兆易创新", "华虹宏力", "寒武纪", "拓荆科技",
    ),
    "pool_e": (
        "中际旭创", "新易盛", "天孚通信", "澜起科技", "兆易创新", "雅克科技", "拓荆科技",
        "联瑞新材", "鼎龙股份", "华海清科", "中科飞测", "长川科技", "华虹宏力",
    ),
    "pool_f": (
        "中际旭创", "新易盛", "天孚通信", "澜起科技", "兆易创新", "雅克科技", "拓荆科技",
        "联瑞新材", "鼎龙股份", "华海清科", "中科飞测", "寒武纪", "长飞光纤", "源杰科技", "东山精密",
    ),
    "pool_g": (
        "中际旭创", "新易盛", "天孚通信", "源杰科技", "光迅科技", "长飞光纤", "澜起科技", "兆易创新",
        "北京君正", "长鑫科技", "寒武纪", "海光信息", "北方华创", "中微公司", "拓荆科技", "盛美上海",
        "华海清科", "芯源微", "中科飞测", "长川科技", "安集科技", "鼎龙股份", "雅克科技", "江丰电子",
        "华特气体", "联瑞新材", "帝尔激光",
    ),
    "pool_h": (
        "澜起科技", "兆易创新", "雅克科技", "拓荆科技", "联瑞新材", "鼎龙股份", "华海清科", "中科飞测",
        "寒武纪", "长飞光纤", "源杰科技", "东山精密",
    ),
    "pool_i": (
        "拓荆科技", "联瑞新材", "鼎龙股份", "华海清科", "中科飞测", "寒武纪", "长飞光纤", "源杰科技", "东山精密",
    ),
    "pool_j": ("寒武纪", "长飞光纤", "源杰科技", "东山精密"),
}


def test_all_requested_research_pools_resolve_by_canonical_name_mapping() -> None:
    assert tuple(UNIVERSE_POOLS) == tuple(EXPECTED_NAMES)
    expected_sizes = {
        "pool_a": 1,
        "pool_b": 3,
        "pool_c": 5,
        "pool_d": 8,
        "pool_e": 13,
        "pool_f": 15,
        "pool_g": 27,
        "pool_h": 12,
        "pool_i": 9,
        "pool_j": 4,
    }
    for pool_name, expected_names in EXPECTED_NAMES.items():
        resolved = symbols_for_pool(pool_name)
        assert tuple(resolved.values()) == expected_names
        assert len(resolved) == expected_sizes[pool_name]
        assert len(resolved) == len(set(resolved))


def test_research_catalog_does_not_expand_the_production_universe() -> None:
    assert len(SYMBOL_NAMES) == 17
    assert tuple(RESEARCH_SYMBOL_NAMES[code] for code in SYMBOL_NAMES) == tuple(
        SYMBOL_NAMES.values()
    )
    assert RESEARCH_SYMBOL_NAMES["688825"] == "长鑫科技"
    assert RESEARCH_SYMBOL_NAMES["688037"] == "芯源微"
    assert "688825" not in SYMBOL_NAMES
    assert "688037" not in SYMBOL_NAMES


def test_every_research_symbol_reuses_existing_routing_and_risk_metadata() -> None:
    expected = set(RESEARCH_SYMBOL_NAMES)
    assert expected <= set(profiles.KNOWN_CLASSIFICATION)
    assert expected <= set(profiles.SYMBOL_GROUPS)
    assert expected <= set(profiles.SYMBOL_PROFILES)
    assert expected <= set(SYMBOL_SUB_INDUSTRY)
    assert SYMBOL_SUB_INDUSTRY["688825"] == "memory"
    assert SYMBOL_SUB_INDUSTRY["688037"] == "equipment"


def test_research_window_defaults_to_2023_and_backtest_accepts_pool_selection() -> None:
    assert DEFAULT_RESEARCH_START_DATE == "2023-01-01"
    parser = build_argument_parser()
    args = parser.parse_args(["--pool", "pool_b", "--no-plot"])
    assert args.pool == "pool_b"
    assert args.start == DEFAULT_RESEARCH_START_DATE

    aliases = parser.parse_args(
        [
            "--pool",
            "pool_f",
            "--start-date",
            "2024-01-01",
            "--end-date",
            "2025-12-31",
            "--no-plot",
        ]
    )
    assert aliases.start == "2024-01-01"
    assert aliases.end == "2025-12-31"


def test_pool_download_selection_includes_all_fixed_risk_evidence() -> None:
    selected = download.select_download_symbols(("pool_b",))
    assert selected[:3] == tuple(symbols_for_pool("pool_b"))
    assert set(PortfolioPolicy().regime_symbols) <= set(selected)
    assert set(RISK_BASKET) <= set(selected)
    assert download.select_download_symbols(()) == download.DEFAULT_SYMBOLS


def test_pool_download_uses_research_window_without_mutating_legacy_defaults() -> None:
    assert download.resolve_download_window(
        "", "", research_selection=True, today="2026-09-16"
    ) == ("2023-01-01", "2026-09-16")
    assert download.resolve_download_window(
        "", "", research_selection=False, today="2026-09-16"
    ) == ("2024-01-01", "2026-07-20")
    assert download.resolve_download_window(
        "2025-04-01", "2025-12-31", research_selection=True, today="2026-09-16"
    ) == ("2025-04-01", "2025-12-31")


def test_pool_download_includes_pre_window_warmup_without_changing_replay_start() -> None:
    assert download.research_data_start(
        "2023-01-01", research_selection=True, warmup_calendar_days=365
    ) == "2022-01-01"
    assert download.research_data_start(
        "2024-01-01", research_selection=False, warmup_calendar_days=365
    ) == "2024-01-01"
    # The retained Eastmoney-only legacy endpoint remains byte-semantically
    # compatible; long research history uses DataFetcher provider failover.
    assert "lmt=1000" in download._url("300308", "2022-01-01", "2026-09-16")


def test_research_fetch_reuses_existing_provider_failover(monkeypatch) -> None:
    index = pd.to_datetime(["2022-01-04", "2022-01-05"])
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.5],
            "close": [10.5, 11.0],
            "high": [11.0, 11.5],
            "low": [9.5, 10.0],
            "volume": [100_000.0, 120_000.0],
        },
        index=index,
    )
    frame.attrs["volume_provider"] = "Sina"

    monkeypatch.setattr(
        download.DataFetcher,
        "fetch_stock_data",
        lambda symbol, start, end: frame,
    )
    monkeypatch.setattr(
        download,
        "_download",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("research mode must not call the legacy Eastmoney-only path")
        ),
    )

    actual, name, provider = download._fetch_research_symbol(
        "300308", "2022-01-01", "2026-09-16"
    )
    assert actual is frame
    assert name == "中际旭创"
    assert provider == "Sina"


def test_unknown_pool_fails_closed() -> None:
    try:
        symbols_for_pool("pool_z")
    except ValueError as exc:
        assert "Unknown research universe" in str(exc)
    else:
        raise AssertionError("unknown pools must fail closed")
