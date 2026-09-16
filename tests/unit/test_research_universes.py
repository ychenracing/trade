"""Research-universe contracts that must not alter the production 17-symbol pool."""

from __future__ import annotations

from quantfusion.application.backtest_cli import build_argument_parser
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
    for pool_name, expected_names in EXPECTED_NAMES.items():
        resolved = symbols_for_pool(pool_name)
        assert tuple(resolved.values()) == expected_names
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


def test_research_window_defaults_to_2023_and_backtest_accepts_pool_selection() -> None:
    assert DEFAULT_RESEARCH_START_DATE == "2023-01-01"
    args = build_argument_parser().parse_args(["--pool", "pool_b", "--no-plot"])
    assert args.pool == "pool_b"
    assert args.start == DEFAULT_RESEARCH_START_DATE


def test_unknown_pool_fails_closed() -> None:
    try:
        symbols_for_pool("pool_z")
    except ValueError as exc:
        assert "Unknown research universe" in str(exc)
    else:
        raise AssertionError("unknown pools must fail closed")
