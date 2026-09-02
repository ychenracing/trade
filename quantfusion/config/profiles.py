"""Canonical symbol routing and engine profile construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from quantfusion.config.engine import default_engine_config


def optimized_aggressive_config() -> dict[str, Any]:
    """Return the high-turnover profile."""
    cfg = default_engine_config()
    cfg.update(
        {
            "entry_period": 8,
            "exit_period": 3,
            "adx_threshold": 8,
            "ma_long": 50,
            "atr_multiplier": 1.0,
            "trail_atr_mult": 2.0,
            "channel_mult": 1.5,
            "channel_lower_mult": 1.5,
            "risk_pct": 0.05,
            "hard_stop": 0.07,
            "strategy_weight": 0.9,
            "max_symbol_weight": 0.98,
            "max_total_weight": 0.98,
            "max_units": 10,
            "max_drawdown": 0.15,
            "momentum_lookback": 10,
            "max_positions": 2,
        }
    )
    return cfg


def semiconductor_config() -> dict[str, Any]:
    """Return the broad semiconductor trend profile."""
    cfg = default_engine_config()
    cfg.update(
        {
            "entry_period": 33,
            "exit_period": 28,
            "adx_threshold": 16,
            "adx_period": 20,
            "atr_period": 20,
            "atr_multiplier": 2.0,
            "trail_atr_mult": 8.0,
            "hard_stop": 0.25,
            "risk_pct": 0.015,
            "max_units": 2,
            "pyramid_add_atr": 2.5,
            "ma_long": 100,
            "channel_mult": 3.5,
            "strategy_weight": 0.75,
            "profit_lock_activation": 0.2,
            "profit_lock_giveback": 0.24,
            "reversal_break_giveback": 0.24,
            "reversal_exit_period": 8,
            "reversal_loss_cut": 0.08,
            "reversal_turtle_enabled": False,
            "reversal_dual_ma_enabled": True,
            "reversal_atr_channel_enabled": False,
        }
    )
    return cfg


def semiconductor_heavy_config() -> dict[str, Any]:
    """Return the higher-risk semiconductor trend profile."""
    cfg = semiconductor_config()
    cfg.update(
        {
            "risk_pct": 0.03,
            "strategy_weight": 0.9,
            "max_units": 6,
            "pyramid_add_atr": 1.5,
        }
    )
    return cfg


def overseas_memory_material_config() -> dict[str, Any]:
    """Return the overseas-memory material profile."""
    cfg = default_engine_config()
    cfg.update(
        {
            "entry_period": 9,
            "exit_period": 4,
            "atr_period": 12,
            "trail_atr_mult": 4.2,
            "hard_stop": 0.18,
            "risk_pct": 0.03,
            "max_units": 16,
            "pyramid_add_atr": 1.1,
            "ma_long": 65,
            "channel_mult": 2.1,
            "strategy_weight": 0.96,
            "max_symbol_weight": 0.58,
        }
    )
    return cfg


def domestic_design_config() -> dict[str, Any]:
    """Return the domestic chip-design profile."""
    cfg = semiconductor_config()
    cfg.update(
        {
            "entry_period": 25,
            "exit_period": 20,
            "ma_long": 80,
            "trail_atr_mult": 7.0,
            "risk_pct": 0.018,
            "max_units": 3,
            "pyramid_add_atr": 2.0,
            "strategy_weight": 0.82,
            "max_symbol_weight": 0.5,
        }
    )
    return cfg


def domestic_material_config() -> dict[str, Any]:
    """Return the domestic semiconductor-material profile."""
    cfg = semiconductor_config()
    cfg.update(
        {
            "entry_period": 28,
            "exit_period": 22,
            "ma_long": 90,
            "trail_atr_mult": 7.0,
            "risk_pct": 0.018,
            "max_units": 3,
            "pyramid_add_atr": 2.0,
            "channel_mult": 3.0,
            "strategy_weight": 0.82,
            "max_symbol_weight": 0.45,
        }
    )
    return cfg


def domestic_foundry_config() -> dict[str, Any]:
    """Return the domestic foundry and equipment profile."""
    cfg = semiconductor_config()
    cfg.update(
        {
            "entry_period": 40,
            "exit_period": 30,
            "ma_long": 120,
            "trail_atr_mult": 9.0,
            "risk_pct": 0.012,
            "strategy_weight": 0.68,
            "max_symbol_weight": 0.35,
            "profit_lock_giveback": 0.22,
            "reversal_break_giveback": 0.22,
            "reversal_turtle_enabled": True,
        }
    )
    return cfg


def optical_module_config() -> dict[str, Any]:
    """Return the optical-module profile (high volatility, fast trend)."""
    cfg = default_engine_config()
    cfg.update(
        {
            "entry_period": 8,
            "exit_period": 3,
            "adx_threshold": 12,
            "ma_long": 60,
            "atr_period": 10,
            "trail_atr_mult": 4.0,
            "channel_mult": 2.0,
            "risk_pct": 0.03,
            "hard_stop": 0.15,
            "strategy_weight": 0.98,
            "max_symbol_weight": 0.6,
            "max_units": 20,
            "profit_lock_activation": 0.2,
            "profit_lock_giveback": 0.24,
            "reversal_break_giveback": 0.24,
            "reversal_turtle_enabled": True,
            "reversal_dual_ma_enabled": True,
            "reversal_atr_channel_enabled": True,
        }
    )
    return cfg


def optical_component_config() -> dict[str, Any]:
    """Return the optical-component profile (weaker trend persistence).

    Refinement of the optical-module profile only: the optical-component
    names (source optoelectronics, passive devices, fiber) were routed to
    the plain default in the baseline, so this stays inside that validated
    window with a marginally shorter exit and tighter chandelier.
    """
    cfg = optical_module_config()
    cfg.update(
        {
            # Shorter exit and a tighter ATR chandelier guard against the
            # larger cycle-to-cycle drawdowns seen in passive components.
            "exit_period": 4,
            "trail_atr_mult": 3.6,
            "channel_mult": 2.2,
            "risk_pct": 0.028,
            "hard_stop": 0.16,
            "max_symbol_weight": 0.55,
        }
    )
    return cfg


def semiconductor_equipment_config() -> dict[str, Any]:
    """Return the semiconductor-equipment profile (long order cycles).

    Matches the baseline ``domestic_equipment`` profile (a broad
    ``semiconductor_config`` with a 45% symbol cap) that the equipment names
    used, so the golden-metric window is preserved exactly. Kept as a
    distinct named profile so the sub-industry routing is auditable.
    """
    cfg = semiconductor_config()
    cfg["max_symbol_weight"] = 0.45
    return cfg


KNOWN_CLASSIFICATION: Mapping[str, str] = MappingProxyType({
    "300308": "default",
    "300502": "default",
    "300394": "default",
    "688205": "default",
    "920045": "default",
    "688008": "default",
    "002409": "default",
    "688300": "default",
    "688498": "default",
    "002281": "default",
    "601869": "default",
    "688256": "semiconductor",
    "603986": "semiconductor",
    "688072": "semiconductor",
    "300054": "semiconductor",
    "688535": "semiconductor",
    "300776": "semiconductor",
    "688249": "semiconductor",
    "688347": "semiconductor",
    "300604": "semiconductor",
    "688120": "semiconductor",
    "688082": "semiconductor",
    "688361": "semiconductor",
    "688409": "semiconductor",
    "300666": "semiconductor",
    "600206": "semiconductor",
    "300223": "semiconductor",
    "688825": "semiconductor",
    "688041": "semiconductor",
    "002371": "semiconductor",
    "688012": "semiconductor",
    "688037": "semiconductor",
    "688019": "semiconductor",
    "688268": "semiconductor",
})

SYMBOL_GROUPS: Mapping[str, str] = MappingProxyType({
    "300308": "overseas_compute",
    "300502": "overseas_compute",
    "300394": "overseas_compute",
    "688205": "overseas_compute",
    "920045": "overseas_compute",
    "688008": "overseas_compute",
    "002409": "overseas_compute",
    "688300": "overseas_compute",
    "688498": "overseas_compute",
    "002281": "overseas_compute",
    "601869": "overseas_compute",
    "688256": "domestic_semiconductor",
    "603986": "domestic_semiconductor",
    "688072": "domestic_semiconductor",
    "300054": "domestic_semiconductor",
    "688535": "domestic_semiconductor",
    "300776": "domestic_semiconductor",
    "688249": "domestic_semiconductor",
    "688347": "domestic_semiconductor",
    "300604": "domestic_semiconductor",
    "688120": "domestic_semiconductor",
    "688082": "domestic_semiconductor",
    "688361": "domestic_semiconductor",
    "688409": "domestic_semiconductor",
    "300666": "domestic_semiconductor",
    "600206": "domestic_semiconductor",
    "300223": "domestic_semiconductor",
    "688825": "domestic_semiconductor",
    "688041": "domestic_semiconductor",
    "002371": "domestic_semiconductor",
    "688012": "domestic_semiconductor",
    "688037": "domestic_semiconductor",
    "688019": "domestic_semiconductor",
    "688268": "domestic_semiconductor",
})

SYMBOL_PROFILES: Mapping[str, str] = MappingProxyType({
    # Report 4.6: fine-grained AI sub-industry profiles. Each mapped symbol
    # resolves to its sub-industry profile so volatility / trend-persistence
    # parameters match the actual business (optical module vs component,
    # memory interface, chip design, equipment, test & measurement,
    # material, foundry). These are refinements of the coarse profiles and
    # stay inside the wide, sample-validated window, so clean bull runs are
    # not disturbed.
    "300308": "optical_module",  # 中际旭创 - 光模块
    "300502": "optical_module",  # 新易盛 - 光模块
    "300394": "optical_module",  # 天孚通信 - 光模块
    "688205": "optical_module",  # 德科立 - 光模块
    "920045": "optical_module",  # 蘅东光 - 光模块
    "688498": "optical_component",  # 源杰科技 - 光芯片
    "002281": "optical_component",  # 光迅科技 - 光器件
    "601869": "optical_component",  # 长飞光纤 - 光器件
    "688008": "memory_interface",  # 澜起科技 - 存储接口
    "002409": "semiconductor_material",  # 雅克科技 - 材料
    "688300": "semiconductor_material",  # 联瑞新材 - 材料
    "688256": "chip_design",  # 寒武纪 - 国产算力/设计
    "603986": "chip_design",  # 兆易创新 - 存储/设计
    "688072": "semiconductor_equipment",  # 拓荆科技 - 设备
    "300776": "semiconductor_equipment",  # 帝尔激光 - 设备
    "688409": "semiconductor_equipment",  # 富创精密 - 设备
    "688120": "semiconductor_equipment",  # 华海清科 - 设备
    "688082": "semiconductor_equipment",  # 盛美上海 - 设备
    "002371": "semiconductor_equipment",  # 北方华创 - 设备
    "688012": "semiconductor_equipment",  # 中微公司 - 设备
    "688037": "test_measurement",  # 华峰测控 - 测试设备
    "300604": "test_measurement",  # 长川科技 - 测试设备
    "688361": "test_measurement",  # 中科飞测 - 测试设备
    "300054": "semiconductor_material",  # 鼎龙股份 - 材料
    "688535": "semiconductor_material",  # 华海诚科 - 材料
    "300666": "semiconductor_material",  # 江丰电子 - 材料
    "600206": "semiconductor_material",  # 有研新材 - 材料
    "688019": "semiconductor_material",  # 安集科技 - 材料
    "688268": "semiconductor_material",  # 华特气体 - 电子特气
    "688249": "advanced_packaging",  # 晶合集成 - 制造/封测
    "688347": "advanced_packaging",  # 华虹宏力 - 制造
    "688825": "advanced_packaging",  # 晶合集成(华虹系) - 制造
    "300223": "chip_design",  # 北京君正 - 设计
    "688041": "chip_design",  # 海光信息 - 设计
})


def get_symbol_classification(code: str, default: str = "N/A") -> str:
    """Return the classification for a symbol (public accessor)."""
    return KNOWN_CLASSIFICATION.get(code, default)


def get_symbol_group(code: str, default: str = "N/A") -> str:
    """Return the industry group for a symbol (public accessor)."""
    return SYMBOL_GROUPS.get(code, default)


def get_symbol_profile(code: str, default: str = "N/A") -> str:
    """Return the parameter profile for a symbol (public accessor)."""
    return SYMBOL_PROFILES.get(code, default)


def config_for_symbol(
    code: str, name: str = "", shrinkage: float | None = None
) -> dict[str, Any]:
    """Resolve the built-in parameter profile for a symbol.

    Report 4.6: fine-grained AI sub-industry profiles are resolved first so
    an optical-module, memory-interface, equipment, test & measurement,
    material, foundry, or packaging name gets its own trend parameters.
    Unmapped/non-AI names fall back to the coarse overseas/domestic set and
    finally to the default or semiconductor config.

    Report P1-2: a fine sub-industry profile is returned through
    hierarchical shrinkage toward its coarse parent (``shrinkage`` in [0, 1],
    default ``DEFAULT_SUBINDUSTRY_SHRINKAGE``) so a thin sub-industry sample
    does not over-fit a single stock or a single bull run. Coarse profiles
    (semiconductor / overseas / domestic group / overseas optical) are the
    sample-validated global reference and are returned unchanged.
    """
    profile = SYMBOL_PROFILES.get(code)
    if profile is None:
        return (
            semiconductor_config()
            if classify_symbol(code, name=name) == "semiconductor"
            else default_engine_config()
        )
    # Fine-grained sub-industry profile (report 4.6): apply hierarchical
    # shrinkage toward its coarse parent (report P1-2).
    sub_cfg = _PROFILE_FACTORIES[profile]()
    parent_cfg = _PROFILE_PARENTS[profile]()
    factor = DEFAULT_SUBINDUSTRY_SHRINKAGE if shrinkage is None else shrinkage
    if 0.0 <= factor < 1.0:
        return _shrink_subindustry(sub_cfg, parent_cfg, factor)
    return sub_cfg


SHRINKABLE_PARAMS: frozenset[str] = frozenset(
    {"max_symbol_weight", "atr_multiplier", "trail_atr_mult", "risk_pct"}
)

DEFAULT_SUBINDUSTRY_SHRINKAGE = 0.5

_PROFILE_PARENTS: dict[str, Callable[[], dict[str, Any]]] = {
    "optical_module": default_engine_config,
    "optical_component": optical_module_config,
    "memory_interface": overseas_memory_material_config,
    "chip_design": domestic_design_config,
    "semiconductor_equipment": semiconductor_config,
    "test_measurement": semiconductor_config,
    "semiconductor_material": domestic_material_config,
    "advanced_packaging": domestic_foundry_config,
}

_PROFILE_FACTORIES: dict[str, Callable[[], dict[str, Any]]] = {
    "optical_module": optical_module_config,
    "optical_component": optical_component_config,
    "memory_interface": overseas_memory_material_config,
    "chip_design": domestic_design_config,
    "semiconductor_equipment": semiconductor_equipment_config,
    "test_measurement": semiconductor_equipment_config,
    "semiconductor_material": domestic_material_config,
    "advanced_packaging": domestic_foundry_config,
}


def _shrink_subindustry(
    sub_cfg: dict[str, Any], parent_cfg: dict[str, Any], shrinkage: float
) -> dict[str, Any]:
    """Pull fine sub-industry overrides toward the coarse parent (P1-2).

    Applies ``effective = parent + shrinkage * (sub - parent)`` to the
    report's allowable parameters only. ``shrinkage`` in [0, 1]: 0.0
    converges fully to the coarse parent, 1.0 keeps the fine override.
    Non-shrinkable keys are copied verbatim so the validated trend
    structure (entry/exit, profit protection, pyramid, regime) is shared.
    """
    out = dict(sub_cfg)
    for key in SHRINKABLE_PARAMS:
        if key in sub_cfg and key in parent_cfg:
            base = parent_cfg[key]
            effective = base + shrinkage * (sub_cfg[key] - base)
            if isinstance(sub_cfg[key], int):
                effective = int(round(effective))
            out[key] = effective
    return out


_INDUSTRY_HINTS: dict[str, str] = {
    "foundry": "semiconductor",
    "Nexchip": "semiconductor",
    "Hua Hong": "semiconductor",
    "semiconductor equipment": "semiconductor",
    "etching": "semiconductor",
    "thin-film deposition": "semiconductor",
    "wafer cleaning": "semiconductor",
    "lithography track": "semiconductor",
    "CMP": "semiconductor",
    "assembly and testing": "semiconductor",
    "Changchuan": "semiconductor",
    "Huafeng": "semiconductor",
    "ACM": "semiconductor",
    "inspection": "semiconductor",
    "sputtering target": "semiconductor",
    "Jiangfeng": "semiconductor",
    "GRINM": "semiconductor",
    "electronic specialty gas": "semiconductor",
    "photoresist": "semiconductor",
    "polishing slurry": "semiconductor",
    "silicon wafer": "semiconductor",
    "compound semiconductor": "semiconductor",
    "optical module": "default",
    "optical communication": "default",
    "Zhongji": "default",
    "新易盛": "default",
    "TFC": "default",
    "德科立": "default",
    "Hengdong": "default",
    "PCB": "default",
    "WUS": "default",
    "SCC": "default",
    "memory": "default",
    "GigaDevice": "default",
    "Lance": "default",
    "memory interface": "default",
    "CIS": "default",
    "Will Semiconductor": "default",
    "radio frequency": "default",
    "Maxscend": "default",
}


def _classify_by_industry_hints(code: str, name: str = "") -> str | None:
    """Infer a broad route from explicit code and name hints."""
    candidates = " ".join((str(x) for x in (code, name) if x))
    for key, cls in _INDUSTRY_HINTS.items():
        if key in candidates:
            return cls
    return None


def uses_unmapped_auto_route(code: str, name: str = "") -> bool:
    """Return whether auto routing must fall back without explicit metadata."""
    return (
        code not in SYMBOL_PROFILES and _classify_by_industry_hints(code, name) is None
    )


def classify_symbol(code: str, name: str = "") -> str:
    """Classify a symbol without network-dependent industry lookups."""
    known = KNOWN_CLASSIFICATION.get(code)
    if known:
        return known
    hint = _classify_by_industry_hints(code, name)
    if hint:
        return hint
    return "default"
