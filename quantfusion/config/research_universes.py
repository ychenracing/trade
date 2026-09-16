"""Configurable research universes kept separate from the production trade pool."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from quantfusion.config.universe import SYMBOL_NAMES

DEFAULT_RESEARCH_START_DATE = "2023-01-01"

# The production 17-symbol mapping remains authoritative for live decision
# support. Research-only symbols below are codes already present in repository
# routing/risk configuration. 688037 and 688825 had stale identity comments in
# older routing metadata; their current security names are recorded here so
# research pools resolve one unambiguous code per requested name.
RESEARCH_SYMBOL_NAMES: Mapping[str, str] = MappingProxyType(
    {
        **SYMBOL_NAMES,
        "688008": "澜起科技",
        "688347": "华虹宏力",
        "002281": "光迅科技",
        "300223": "北京君正",
        "688825": "长鑫科技",
        "688041": "海光信息",
        "002371": "北方华创",
        "688012": "中微公司",
        "688037": "芯源微",
        "688019": "安集科技",
        "300666": "江丰电子",
        "688268": "华特气体",
        "300776": "帝尔激光",
    }
)

# Official first-trading dates are used only to identify windows that end before
# a late-listed symbol could possibly have market data. They never create or
# forward-fill pre-listing observations.
RESEARCH_FIRST_TRADING_DATES: Mapping[str, str] = MappingProxyType(
    {
        "688535": "2023-04-04",  # 华海诚科
        "688249": "2023-05-05",  # 晶合集成
        "688361": "2023-05-19",  # 中科飞测
        "688347": "2023-08-07",  # 华虹公司
        "920045": "2025-12-31",  # 蘅东光
        "688825": "2026-07-27",  # 长鑫科技
    }
)

_NAME_TO_SYMBOL = {name: code for code, name in RESEARCH_SYMBOL_NAMES.items()}
if len(_NAME_TO_SYMBOL) != len(RESEARCH_SYMBOL_NAMES):
    raise RuntimeError("Research symbol catalog contains duplicate security names")


@dataclass(frozen=True)
class UniversePool:
    """One named research universe resolved to ordered six-digit symbols."""

    name: str
    description: str
    symbols: tuple[str, ...]


_POOL_MEMBER_NAMES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
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
)

_POOL_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "pool_a": "single optical-communications leader",
        "pool_b": "optical-communications core",
        "pool_c": "optical core plus memory and chip design",
        "pool_d": "optical, memory, domestic compute and equipment",
        "pool_e": "broader semiconductor equipment and materials chain",
        "pool_f": "optical and semiconductor diversified technology pool",
        "pool_g": "full technology-industry-chain research pool",
        "pool_h": "semiconductor and optical-component diversified pool",
        "pool_i": "equipment, materials, compute and optical components",
        "pool_j": "compact compute and optical-component pool",
    }
)


def _resolve_names(names: tuple[str, ...]) -> tuple[str, ...]:
    if len(names) != len(set(names)):
        raise ValueError("Research universe contains duplicate security names")
    missing = [name for name in names if name not in _NAME_TO_SYMBOL]
    if missing:
        raise ValueError(f"Missing symbol mapping for research names: {', '.join(missing)}")
    symbols = tuple(_NAME_TO_SYMBOL[name] for name in names)
    if len(symbols) != len(set(symbols)):
        raise ValueError("Research universe resolves multiple names to the same symbol")
    return symbols


UNIVERSE_POOLS: Mapping[str, UniversePool] = MappingProxyType(
    {
        name: UniversePool(
            name=name,
            description=_POOL_DESCRIPTIONS[name],
            symbols=_resolve_names(member_names),
        )
        for name, member_names in _POOL_MEMBER_NAMES.items()
    }
)


def _normalize_pool_name(name: str) -> str:
    key = name.strip().lower().replace("-", "_")
    if len(key) == 1 and "a" <= key <= "j":
        key = f"pool_{key}"
    return key


def get_universe_pool(name: str) -> UniversePool:
    """Return a configured research pool or fail closed for unknown input."""
    key = _normalize_pool_name(name)
    try:
        return UNIVERSE_POOLS[key]
    except KeyError as exc:
        available = ", ".join(UNIVERSE_POOLS)
        raise ValueError(
            f"Unknown research universe: {name!r}; available: {available}"
        ) from exc


def symbols_for_pool(name: str) -> dict[str, str]:
    """Return an ordered code-to-name mapping for one research pool."""
    pool = get_universe_pool(name)
    return {code: RESEARCH_SYMBOL_NAMES[code] for code in pool.symbols}
