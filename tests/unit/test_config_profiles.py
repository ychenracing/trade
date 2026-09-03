"""Exact contracts for the canonical engine profile facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pytest

from quantfusion.application import engine_api
from quantfusion.config import profiles
from quantfusion.config.engine import default_engine_config, validate_engine_config
from quantfusion.engine.core import CoreBacktestEngine


PROFILE_HASHES = {
    "optimized_aggressive_config": "ebd638f6da749775509ef669e9c412c616a0fdfc772e1874195f80d3b496ba49",
    "semiconductor_config": "b8b993613b11ac837bb7422c6d0b1d468632afe78f30cd282721f87fd154206e",
    "semiconductor_heavy_config": "be92dc9d48b590681e611a46fc55d97987de46ed1043b4905387b8e7933b938a",
    "overseas_memory_material_config": "d915ac46e412fa3613669783a4e843a1e48ac0a9946c343966ddb965c624522c",
    "domestic_design_config": "4bf88659921aaa008fc2b9d0755312d0b149723232c1bef10c85c2180e464d34",
    "domestic_material_config": "496273347457c1568f9ea89939c41f89a2dc451e365a1280f13c462b4ca24ff3",
    "domestic_foundry_config": "c40d565e3ec045175a3f06cd3082f47f3a32686263ae2e429293d5f5ab04ea27",
    "optical_module_config": "41a95b4b9c6e6bdfc1f0b64e8c1e2568c93e8948b62d9c0bf22106c05f03fed9",
    "optical_component_config": "47fbd36d6225fb72626fc8dceb3dc5c6ceb8ed8b482b1261ecda2e3dd943d7ad",
    "semiconductor_equipment_config": "82512db363a49e100523209bb67c678e3e1e32b3980a46424f6afd045943f364",
}


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (set, frozenset)):
        return sorted(_normalize(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def test_every_named_profile_has_exact_pre_refactor_content() -> None:
    for name, expected in PROFILE_HASHES.items():
        assert _digest(getattr(profiles, name)()) == expected


def test_every_mapped_symbol_has_exact_effective_profile_at_default_shrinkage() -> None:
    effective = {
        code: profiles.config_for_symbol(code)
        for code in sorted(profiles.SYMBOL_PROFILES)
    }
    assert len(effective) == 36
    assert _digest(effective) == (
        "872ecbc6e5e0fc478ad81aff5f7775f499cbdb7642925c26cc23335a870575f2"
    )


def test_symbol_routing_fact_sets_are_exact() -> None:
    assert _digest(profiles.KNOWN_CLASSIFICATION) == (
        "b1ed03255f1b896baac27065e595dd67b4f1e574931727e78ba96f31ccdd7bb2"
    )
    assert _digest(profiles.SYMBOL_GROUPS) == (
        "c99c880825820e2778d41a24e2dddb091de79696c1c31d7399063dbc2c409504"
    )
    assert _digest(profiles.SYMBOL_PROFILES) == (
        "d16f148d723542a52ec72726af967b8e6dfd124420c33441718ccf9355579e3d"
    )


def test_symbol_routing_fact_sets_are_immutable() -> None:
    for mapping in (
        profiles.KNOWN_CLASSIFICATION,
        profiles.SYMBOL_GROUPS,
        profiles.SYMBOL_PROFILES,
    ):
        with pytest.raises(TypeError):
            mapping["999999"] = "mutated"  # type: ignore[index]


def test_default_config_content_and_validation_are_exact() -> None:
    defaults = default_engine_config()
    assert _digest(defaults) == (
        "9f12be9c503bf493dd3a8c9b8cbb6169d981746f7b2417db2bfb38d49c7f6dca"
    )
    assert validate_engine_config(defaults) == defaults


def test_engine_has_no_profile_or_config_builders_and_accessors() -> None:
    removed = {
        "_default_config",
        "_PER_SYMBOL_OVERRIDE_KEYS",
        "_validate_config",
        "config_for_symbol",
        "get_symbol_classification",
        "get_symbol_group",
        "get_symbol_profile",
        "optimized_aggressive_config",
        "semiconductor_config",
        "semiconductor_heavy_config",
    }
    assert not {name for name in removed if hasattr(CoreBacktestEngine, name)}


def test_application_symbol_accessors_keep_their_current_defaults() -> None:
    assert not hasattr(engine_api, "get_symbol_classification")
    assert engine_api.get_symbol_group("999999") == "default"
    assert engine_api.get_symbol_profile("999999") == "default"
    assert engine_api.get_symbol_group("300308") == "overseas_compute"
    assert engine_api.get_symbol_profile("300308") == "optical_module"
