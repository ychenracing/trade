"""守卫当前 17 股、958 场景正式压力合同及其证据一致性。"""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tokenize
from typing import Any

from quantfusion.application import stress_scenarios
from quantfusion.config import daily
from quantfusion.config.universe import SYMBOL_NAMES


ROOT = Path(__file__).resolve().parents[2]
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
EXPECTED_FAMILIES = {
    "prefix": 17,
    "leave_one_out": 17,
    "add_one": 24,
    "random_subset": 750,
    "permutation": 150,
}
CURRENT_PLAN_START = "<!-- CURRENT_FORMAL_STRESS_PLAN:START -->"
CURRENT_PLAN_END = "<!-- CURRENT_FORMAL_STRESS_PLAN:END -->"
CURRENT_RESULT_START = "<!-- CURRENT_FORMAL_STRESS_RESULT:START -->"
CURRENT_RESULT_END = "<!-- CURRENT_FORMAL_STRESS_RESULT:END -->"
CURRENT_DOCUMENTS = (
    Path("README.md"),
    Path("docs/VALIDATION.md"),
    Path("docs/ARCHITECTURE.md"),
)
HISTORICAL_QUALIFIERS = (
    "历史",
    "旧",
    "22 股",
    "22股",
    "22-symbol",
    "historical",
    "legacy",
    "prior",
    "previous",
    "immutable",
)
TEMPORARY_GLOBS = (
    ".github/task-bootstrap/*formal-stress-958*",
    ".github/workflows/*formal-stress-958*.yml",
    "artifacts/validation/formal_stress_958_*checkpoint*.md",
    "scripts/_formal_stress_958*",
)
HEX_40 = re.compile(r"[0-9a-f]{40}")
HEX_64 = re.compile(r"[0-9a-f]{64}")


def _managed_block(text: str, start: str, end: str, *, path: Path) -> str:
    assert text.count(start) == 1, path
    assert text.count(end) == 1, path
    _, rest = text.split(start, 1)
    block, _ = rest.split(end, 1)
    return block


def _assert_983_is_historical(text: str, *, path: Path) -> None:
    """要求每个 983 引用在本行或所属 Markdown 小节中明确标为历史。"""
    heading = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            heading = line
        if "983" not in line:
            continue
        context = f"{heading}\n{line}".lower()
        assert any(
            qualifier.lower() in context for qualifier in HISTORICAL_QUALIFIERS
        ), (path, line)


def _python_comments_and_docstrings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    comments = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    tree = ast.parse(source, filename=str(path))
    docstrings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            value = ast.get_docstring(node, clean=False)
            if value:
                docstrings.append(value)
    return comments + docstrings


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path
    return payload


def test_current_formal_plan_is_exactly_17_symbols_and_958_scenarios() -> None:
    assert tuple(SYMBOL_NAMES) == EXPECTED_SYMBOLS
    assert tuple(daily.SYMBOLS) == EXPECTED_SYMBOLS
    scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )
    ids = [str(item["scenario_id"]) for item in scenarios]
    assert len(scenarios) == 958
    assert len(set(ids)) == 958
    assert Counter(str(item["scenario_type"]) for item in scenarios) == Counter(
        EXPECTED_FAMILIES
    )


def test_current_documentation_separates_958_from_historical_983() -> None:
    for relative in CURRENT_DOCUMENTS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        plan_block = _managed_block(
            text,
            CURRENT_PLAN_START,
            CURRENT_PLAN_END,
            path=relative,
        )
        result_block = _managed_block(
            text,
            CURRENT_RESULT_START,
            CURRENT_RESULT_END,
            path=relative,
        )
        assert "958" in plan_block, relative
        assert "17 股" in plan_block or "17股" in plan_block, relative
        for family in EXPECTED_FAMILIES:
            assert family.replace("_", "-") in plan_block, (relative, family)
        assert "983" not in result_block, relative
        _assert_983_is_historical(text, path=relative)


def test_python_comments_and_docstrings_do_not_call_983_current() -> None:
    for root in (ROOT / "quantfusion", ROOT / "scripts"):
        for path in root.rglob("*.py"):
            for text in _python_comments_and_docstrings(path):
                if "983" not in text:
                    continue
                lowered = text.lower()
                assert any(
                    qualifier.lower() in lowered
                    for qualifier in HISTORICAL_QUALIFIERS
                ), (path.relative_to(ROOT), text)


def test_recorded_958_summary_and_candidate_are_complete_and_consistent() -> None:
    summary_path = (
        ROOT / "artifacts/validation/formal_stress_958_acceptance_summary.json"
    )
    assert summary_path.is_file()
    summary = _load_json(summary_path)
    assert summary["scenario_count"] == 958
    assert summary["unique_scenario_ids"] == 958
    assert summary["family_counts"] == EXPECTED_FAMILIES
    assert summary["formal_exit_status"] in {0, 2}
    assert summary["acceptance_status"] in {"accepted", "rejected"}
    assert isinstance(summary["canonical"], bool)
    assert HEX_40.fullmatch(str(summary["source_revision"]))
    assert HEX_64.fullmatch(str(summary["candidate_sha256"]))

    if summary["acceptance_status"] == "rejected":
        assert summary["formal_exit_status"] == 2
        assert summary["canonical"] is False
    else:
        assert summary["formal_exit_status"] == 0

    for gate_name in (
        "absolute_hard_gates",
        "retained_robustness_hard_gates",
    ):
        gates = summary[gate_name]
        assert isinstance(gates, dict)
        assert isinstance(gates.get("passed"), bool)

    provenance = summary["provenance"]
    assert isinstance(provenance, dict)
    for key in (
        "source_fingerprint",
        "data_fingerprint",
        "scenario_signature",
        "run_signature",
    ):
        assert isinstance(provenance[key], str)
        assert len(provenance[key]) >= 32

    candidate_path = ROOT / str(summary["candidate_path"])
    assert candidate_path.is_file()
    assert hashlib.sha256(candidate_path.read_bytes()).hexdigest() == summary[
        "candidate_sha256"
    ]
    candidate = _load_json(candidate_path)
    assert candidate["source_revision"] == summary["source_revision"]
    assert candidate["scenario_count"] == 958
    assert candidate["acceptance_status"] == summary["acceptance_status"]
    assert candidate["canonical"] == summary["canonical"]
    for key, value in provenance.items():
        assert candidate[key] == value

    results = candidate["results"]
    assert isinstance(results, list)
    assert len(results) == 958
    assert len({str(item["scenario_id"]) for item in results}) == 958
    assert Counter(str(item["scenario_type"]) for item in results) == Counter(
        EXPECTED_FAMILIES
    )
    for item in results:
        assert item["deployment_policy"] == "production_daily_replay"
        for key in ("total_return", "max_drawdown", "sharpe", "calmar"):
            assert math.isfinite(float(item[key])), (item["scenario_id"], key)

    for relative in CURRENT_DOCUMENTS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        result_block = _managed_block(
            text,
            CURRENT_RESULT_START,
            CURRENT_RESULT_END,
            path=relative,
        )
        assert summary["source_revision"] in result_block, relative
        assert summary["candidate_sha256"] in result_block, relative
        assert str(summary["candidate_path"]) in result_block, relative


def test_temporary_acceptance_and_consistency_infrastructure_is_absent() -> None:
    leftovers = sorted(
        {
            path.relative_to(ROOT).as_posix()
            for pattern in TEMPORARY_GLOBS
            for path in ROOT.glob(pattern)
        }
    )
    assert leftovers == []
