# Current plan and explicitly historical baseline documentation contracts.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy


ROOT = Path(__file__).resolve().parents[2]
UNIVERSES = (
    ("1_symbol", "1"),
    ("3_symbols", "3"),
    ("5_symbols", "5"),
    ("13_symbols", "13"),
    ("17_symbols", "17"),
)


def _warm_result(name: str) -> dict[str, object]:
    payload = json.loads(
        (ROOT / "artifacts/validation/universe_backtest.json").read_text(
            encoding="utf-8"
        )
    )
    matches = [
        item
        for item in payload["results"]
        if item.get("universe") == name
        and item.get("indicator_state") == "warm"
        and item.get("end_date") == "2026-07-20"
    ]
    assert len(matches) == 1
    return matches[0]


def _declared_parameter_names(text: str, heading: str, label: str) -> set[str]:
    """Inspect the declared list itself, not incidental mentions elsewhere."""
    sections = re.findall(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)",
        text, flags=re.MULTILINE | re.DOTALL,
    )
    assert len(sections) == 1, heading
    lists = re.findall(rf"^{re.escape(label)}：([^\n]+)$", sections[0], re.MULTILINE)
    assert len(lists) == 1, label
    names = re.findall(r"`([a-z][a-z0-9_]*)`", lists[0])
    assert names and len(names) == len(set(names)), names
    return set(names)


def _assert_parameter_list(text: str, heading: str, label: str, expected: set[str]) -> None:
    assert _declared_parameter_names(text, heading, label) == expected


def test_declared_parameter_lists_cover_their_actual_complete_scope() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    _assert_parameter_list(readme, "默认策略参数", "策略参数", set(default_engine_config()))
    _assert_parameter_list(readme, "组合策略参数", "组合参数", set(PortfolioPolicy.__dataclass_fields__))


def test_incidental_parameter_mention_cannot_hide_a_declared_list_omission() -> None:
    text = (
        "开关 `account_risk_budget_enabled` 默认开启。\n"
        "## 默认策略参数\n策略参数：`entry_period`。\n"
        "## 其他\n这里再次提到 `account_risk_budget_enabled`。\n"
    )
    expected = {"entry_period", "account_risk_budget_enabled"}
    with pytest.raises(AssertionError):
        _assert_parameter_list(text, "默认策略参数", "策略参数", expected)
    corrected = text.replace("策略参数：`entry_period`。", "策略参数：`entry_period`、`account_risk_budget_enabled`。")
    _assert_parameter_list(corrected, "默认策略参数", "策略参数", expected)


@pytest.mark.parametrize("text", [
    "## 默认策略参数\n没有清单。\n",
    "## 默认策略参数\n策略参数：`entry_period`、`entry_period`。\n",
    "## 默认策略参数\n策略参数：`entry_period`。\n策略参数：`exit_period`。\n",
    "## 默认策略参数\n策略参数：`entry_period`。\n## 默认策略参数\n策略参数：`entry_period`。\n",
])
def test_malformed_parameter_lists_are_rejected(text: str) -> None:
    with pytest.raises(AssertionError):
        _declared_parameter_names(text, "默认策略参数", "策略参数")


def test_current_baseline_tables_match_the_frozen_artifact() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    validation = (ROOT / "docs/VALIDATION.md").read_text(encoding="utf-8")
    assert "[验证结果与证据](docs/VALIDATION.md#formal-stress-evidence)" in readme
    assert validation.count('<a id="formal-stress-evidence"></a>') == 1
    assert "## 历史 pre-C6 生产趋势基线" in validation
    for name, label in UNIVERSES:
        item = _warm_result(name)
        row = (
            f"| {label} | {float(item['total_return']):.4%} | "
            f"{float(item['max_drawdown']):.4%} | "
            f"{int(item['total_trades'])} | "
            f"{int(item['date_symbol_side_count'])} |"
        )
        assert row in validation


def test_current_plan_wording_is_not_mixed_with_historical_983_evidence() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    validation = (ROOT / "docs/VALIDATION.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    data_readme = (ROOT / "data/README.md").read_text(encoding="utf-8")
    combined_current = "\n".join((readme, validation, architecture))
    assert "1/3/5/13/22" not in combined_current
    assert "共 983 次生产逐日回放" not in combined_current
    assert "全部 983 个正式场景" not in combined_current
    assert "精确 canonical 983" not in validation
    assert "983 场景计划能够进入正式发布校验" not in validation
    assert "add-one-05-688072" in readme
    assert "当前 17 只交易股票" in data_readme
    assert "历史 22 股完整 983 场景" in validation


def test_final_result_block_replaces_pending_text_after_publication() -> None:
    summary = (
        ROOT
        / "artifacts/validation/formal_stress_958_acceptance_summary.json"
    )
    if not summary.exists():
        return
    validation = (ROOT / "docs/VALIDATION.md").read_text(encoding="utf-8")
    assert "完整计划已运行：`958/958`" in validation
    assert validation.count('<a id="formal-stress-evidence"></a>') == 1
    links = {
        "README.md": "docs/VALIDATION.md#formal-stress-evidence",
        "docs/ARCHITECTURE.md": "VALIDATION.md#formal-stress-evidence",
    }
    for relative in (
        "README.md",
        "docs/VALIDATION.md",
        "docs/ARCHITECTURE.md",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert (
            "完整 958 场景尚未在本次任务中形成最终工件" not in text
        )
        if relative in links:
            assert f"[验证结果与证据]({links[relative]})" in text
