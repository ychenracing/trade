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
import subprocess
import tokenize
from typing import Any

from quantfusion import config as qf
from quantfusion.application import stress_artifacts, stress_metrics, stress_scenarios
from quantfusion.config import daily
from quantfusion.config.paths import MARKET_DATA_DIR, REGIME_DATA_DIR
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
EXPECTED_SOURCE_REVISION = "acf4cccf4117edb35e6beb57aa2f9004476c8b93"
FAMILY_LABELS = {
    "prefix": "prefix",
    "leave_one_out": "leave-one-out",
    "add_one": "add-one",
    "random_subset": "random-subset",
    "permutation": "permutation",
}
CURRENT_PLAN_START = "<!-- CURRENT_FORMAL_STRESS_PLAN:START -->"
CURRENT_PLAN_END = "<!-- CURRENT_FORMAL_STRESS_PLAN:END -->"
CURRENT_RESULT_START = "<!-- CURRENT_FORMAL_STRESS_RESULT:START -->"
CURRENT_RESULT_END = "<!-- CURRENT_FORMAL_STRESS_RESULT:END -->"
PLAN_META = re.compile(
    r"^<!-- CURRENT_FORMAL_STRESS_PLAN_META: (\{.*\}) -->$",
    re.MULTILINE,
)
CURRENT_DOCUMENTS = (
    Path("README.md"),
    Path("docs/VALIDATION.md"),
    Path("docs/ARCHITECTURE.md"),
)
DETAILED_PLAN_DOCUMENTS = (
    Path("README.md"),
    Path("docs/VALIDATION.md"),
)
GATE_FIELDS = (
    "absolute_hard_gates",
    "retained_robustness_hard_gates",
    "robustness_diagnostics",
    "promotion_gates",
    "initial_baseline_gates",
)
REPLAY_FIELDS = (
    "engine",
    "deployment_policy",
    "trade_count_semantics",
    "portfolio_policy",
    "data_directory",
    "regime_data_directory",
    "indicator_state",
    "seeds",
    "random_samples_per_size_per_seed",
    "permutation_samples_per_seed",
)
CURRENT_PLAN_COUNT_LINE = (
    "当前计划计数：17 股；958 场景；prefix=17；leave-one-out=17；"
    "add-one=24；random-subset=750；permutation=150。"
)
TEMPORARY_GLOBS = (
    ".github/task-bootstrap/*formal-stress-958*",
    ".github/workflows/*formal-stress-958*.yml",
    "artifacts/validation/formal_stress_958_*checkpoint*.md",
    "scripts/_formal_stress_958*",
)
HISTORICAL_TOKEN = re.compile(r"历史|historical", re.IGNORECASE)
SYMBOL_22_TOKEN = re.compile(r"22\s*股|22-symbol", re.IGNORECASE)
CURRENT_TOKEN = re.compile(r"当前|现行|\b(?:current|active)\b", re.IGNORECASE)
SCENARIO_983_TOKEN = re.compile(r"(?<![0-9A-Za-z])983(?![0-9A-Za-z])")
HEX_40 = re.compile(r"[0-9a-f]{40}")
HEX_64 = re.compile(r"[0-9a-f]{64}")


def _managed_block(text: str, start: str, end: str, *, path: Path) -> str:
    assert text.count(start) == 1, path
    assert text.count(end) == 1, path
    _, rest = text.split(start, 1)
    block, _ = rest.split(end, 1)
    return block


def _assert_983_is_historical(text: str, *, path: Path) -> None:
    """Require explicit historical 22-symbol context for every old-plan mention."""
    normalized = re.sub(
        r"(历史|historical)\s*[,，]\s*(22\s*股|22-symbol)",
        r"\1 \2",
        text,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(22\s*股|22-symbol)\s*[,，]\s*(历史|historical)",
        r"\1 \2",
        normalized,
        flags=re.IGNORECASE,
    )
    clauses = re.split(
        r"[。！？.!?；;：:，,\n]+",
        normalized,
        flags=re.IGNORECASE,
    )
    for raw in clauses:
        clause = raw.strip()
        occurrences = list(SCENARIO_983_TOKEN.finditer(clause))
        for occurrence in occurrences:
            prefix = clause[: occurrence.start()]
            historical = list(HISTORICAL_TOKEN.finditer(prefix))
            symbol_22 = list(SYMBOL_22_TOKEN.finditer(prefix))
            current = list(CURRENT_TOKEN.finditer(prefix))
            assert historical and symbol_22, (path, clause)
            last_current = current[-1].start() if current else -1
            assert historical[-1].start() > last_current, (path, clause)
            assert symbol_22[-1].start() > last_current, (path, clause)


def _assert_22_symbol_is_historical(text: str, *, path: Path) -> None:
    for raw in re.split(r"[。！？.!?；;，,\n]+", text):
        sentence = raw.strip()
        if not SYMBOL_22_TOKEN.search(sentence):
            continue
        assert HISTORICAL_TOKEN.search(sentence), (path, sentence)


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


def _current_scenarios() -> list[dict[str, Any]]:
    return stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )


def _plan_metadata(plan_block: str, *, path: Path) -> dict[str, Any]:
    matches = PLAN_META.findall(plan_block)
    assert len(matches) == 1, path
    payload = json.loads(matches[0])
    assert isinstance(payload, dict), path
    return payload


def _assert_visible_plan_counts(plan_block: str, *, path: Path) -> None:
    assert CURRENT_PLAN_COUNT_LINE in plan_block, path
    visible = PLAN_META.sub("", plan_block)
    before, rest = visible.split(CURRENT_RESULT_START, 1)
    _, after = rest.split(CURRENT_RESULT_END, 1)
    visible = before + after
    current_clauses = [
        clause
        for clause in re.split(
            r"[。！？.!?；;：:，,\n]+|\b(?:and|but|while)\b|"
            r"(?:并且|而且|但是|但|同时|以及|与|和)",
            visible,
            flags=re.IGNORECASE,
        )
        if not (
            HISTORICAL_TOKEN.search(clause)
            and SYMBOL_22_TOKEN.search(clause)
        )
    ]
    current_text = "\n".join(current_clauses)
    for family, expected in EXPECTED_FAMILIES.items():
        label = re.escape(FAMILY_LABELS[family])
        counts = {
            int(match)
            for match in re.findall(
                rf"(\d+)\s*个\s+(?:deterministic\s+)?{label}",
                current_text,
            )
        }
        counts.update(
            int(match)
            for match in re.findall(rf"{label}\s*=\s*(\d+)", current_text)
        )
        assert counts == {expected}, (path, family, counts)
    symbol_counts = {
        int(value) for value in re.findall(r"(\d+)\s*股", current_text)
    }
    scenario_counts = {
        int(value) for value in re.findall(r"(\d+)\s*(?:个\s*)?场景", current_text)
    }
    assert symbol_counts == {17}, (path, symbol_counts)
    assert scenario_counts == {958}, (path, scenario_counts)


def _tracked_python_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [
        ROOT / raw.decode()
        for raw in completed.stdout.split(b"\0")
        if raw
    ]


def _expected_result_text(summary: dict[str, Any]) -> str:
    worst = summary["worst_all"]
    prefix = summary["prefix_17"]
    return (
        "完整计划已运行：`958/958`，唯一 scenario ID：`958`。"
        f"工件状态为 `{summary['artifact_status']}`，"
        f"acceptance 为 `{summary['acceptance_status']}`，"
        f"canonical 为 `{str(summary['canonical']).lower()}`；"
        "absolute hard gates passed="
        f"`{summary['absolute_hard_gates']['passed']}`，"
        "retained robustness gates passed="
        f"`{summary['retained_robustness_hard_gates']['passed']}`。"
        f"全场景最差最大回撤为 `{worst['max_drawdown']:.6%}`"
        f"（`{worst['scenario_id']}`），17 股完整 prefix 的总收益为 "
        f"`{prefix['total_return']:.6%}`、最大回撤为 "
        f"`{prefix['max_drawdown']:.6%}`。当前候选："
        f"`{summary['candidate_path']}`，SHA-256："
        f"`{summary['candidate_sha256']}`；source revision："
        f"`{summary['source_revision']}`。详细 gates 与 provenance 见 "
        "`artifacts/validation/formal_stress_958_acceptance_summary.json`。"
    )


def test_current_formal_plan_is_exactly_17_symbols_and_958_scenarios() -> None:
    assert tuple(SYMBOL_NAMES) == EXPECTED_SYMBOLS
    assert tuple(daily.SYMBOLS) == EXPECTED_SYMBOLS
    scenarios = _current_scenarios()
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
        assert _plan_metadata(plan_block, path=relative) == {
            "symbol_count": 17,
            "scenario_count": 958,
            "family_counts": EXPECTED_FAMILIES,
        }
        _assert_visible_plan_counts(plan_block, path=relative)
        if relative in DETAILED_PLAN_DOCUMENTS:
            for family in EXPECTED_FAMILIES:
                assert FAMILY_LABELS[family] in plan_block, (relative, family)
        assert SCENARIO_983_TOKEN.search(result_block) is None, relative
        _assert_983_is_historical(text, path=relative)
        _assert_22_symbol_is_historical(text, path=relative)


def test_all_python_comments_and_docstrings_mark_983_as_historical_22() -> None:
    for path in _tracked_python_files():
        for text in _python_comments_and_docstrings(path):
            if "983" in text:
                _assert_983_is_historical(
                    text,
                    path=path.relative_to(ROOT),
                )


def test_recorded_958_summary_candidate_and_docs_are_one_contract() -> None:
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
    assert summary["source_revision"] == EXPECTED_SOURCE_REVISION
    assert HEX_64.fullmatch(str(summary["candidate_sha256"]))

    if summary["acceptance_status"] == "rejected":
        assert summary["formal_exit_status"] == 2
        assert summary["canonical"] is False
        assert summary["artifact_status"] == "current_candidate"
        assert summary["rejection_reasons"]
    else:
        assert summary["formal_exit_status"] == 0
        assert summary["canonical"] is True
        assert summary["rejection_reasons"] == []

    for gate_name in GATE_FIELDS:
        assert isinstance(summary[gate_name], dict), gate_name
    for gate_name in (
        "absolute_hard_gates",
        "retained_robustness_hard_gates",
    ):
        assert isinstance(summary[gate_name].get("passed"), bool), gate_name

    scenarios = _current_scenarios()
    expected_provenance = stress_artifacts._build_provenance(
        scenarios,
        MARKET_DATA_DIR.resolve(),
        REGIME_DATA_DIR.resolve(),
        source_revision=summary["source_revision"],
    )
    assert summary["provenance"] == {
        field: expected_provenance[field]
        for field in stress_artifacts.PROVENANCE_FIELDS
    }

    candidate_relative = Path(str(summary["candidate_path"]))
    assert not candidate_relative.is_absolute()
    candidate_path = (ROOT / candidate_relative).resolve()
    assert candidate_path.is_relative_to((ROOT / "artifacts/validation").resolve())
    assert candidate_path.is_file()
    assert hashlib.sha256(candidate_path.read_bytes()).hexdigest() == summary[
        "candidate_sha256"
    ]
    candidate = _load_json(candidate_path)

    assert candidate["source_revision"] == summary["source_revision"]
    assert candidate["scenario_count"] == 958
    assert candidate["artifact_status"] == summary["artifact_status"]
    assert candidate["acceptance_status"] == summary["acceptance_status"]
    assert candidate["canonical"] == summary["canonical"]
    assert candidate.get("rejection_reasons", []) == summary["rejection_reasons"]
    assert summary["provenance"] == {
        field: candidate[field] for field in stress_artifacts.PROVENANCE_FIELDS
    }
    assert summary["replay_contract"] == {
        field: candidate[field] for field in REPLAY_FIELDS
    }
    assert candidate["engine"] == "ProductionReplayEngine"
    assert candidate["deployment_policy"] == "production_daily_replay"
    assert candidate["trade_count_semantics"] == "trade_records"
    assert candidate["portfolio_policy"] == qf.PortfolioPolicy().as_dict()
    assert candidate["data_directory"] == stress_artifacts._artifact_path(
        MARKET_DATA_DIR.resolve()
    )
    assert candidate["regime_data_directory"] == stress_artifacts._artifact_path(
        REGIME_DATA_DIR.resolve()
    )
    assert candidate["indicator_state"] == "warm"
    assert candidate["seeds"] == list(stress_scenarios.DEFAULT_SEEDS)
    assert (
        candidate["random_samples_per_size_per_seed"]
        == stress_scenarios.DEFAULT_RANDOM_SAMPLES
    )
    assert (
        candidate["permutation_samples_per_seed"]
        == stress_scenarios.DEFAULT_PERMUTATION_SAMPLES
    )
    for gate_name in GATE_FIELDS:
        assert candidate[gate_name] == summary[gate_name], gate_name

    results = candidate["results"]
    assert isinstance(results, list)
    assert len(results) == 958
    result_ids = {str(item["scenario_id"]) for item in results}
    assert len(result_ids) == 958
    assert result_ids == {str(item["scenario_id"]) for item in scenarios}
    assert Counter(str(item["scenario_type"]) for item in results) == Counter(
        EXPECTED_FAMILIES
    )
    validated = stress_artifacts._validated_checkpoint_results(
        {"results": results},
        scenarios,
    )
    assert set(validated) == result_ids
    for item in results:
        assert item["deployment_policy"] == "production_daily_replay"
        for key in ("total_return", "max_drawdown", "sharpe", "calmar"):
            value = item[key]
            assert type(value) in (int, float), (item["scenario_id"], key)
            assert math.isfinite(value), (item["scenario_id"], key)

    assert candidate["absolute_hard_gates"] == (
        stress_metrics._absolute_hard_gates(results)
    )
    assert candidate["retained_robustness_hard_gates"] == (
        stress_metrics._retained_robustness_hard_gates(results)
    )
    assert candidate["robustness_diagnostics"] == (
        stress_metrics._robustness_diagnostics(results)
    )
    incumbent = stress_artifacts._load_incumbent(
        stress_artifacts.VALIDATION_ARTIFACT_DIR / "universe_stress.json"
    )
    assert incumbent is None
    assert candidate["promotion_gates"] == stress_metrics._promotion_gates(
        results,
        incumbent,
    )
    assert candidate["initial_baseline_gates"] == (
        stress_metrics._initial_baseline_gates(results, None)
    )
    route_accepted = (
        stress_metrics._promotion_accepted(candidate["promotion_gates"])
        if incumbent is not None
        else False
    )
    expected_accepted = (
        candidate["absolute_hard_gates"]["passed"]
        and candidate["retained_robustness_hard_gates"]["passed"]
        and route_accepted
    )
    expected_status = "accepted" if expected_accepted else "rejected"
    assert summary["acceptance_status"] == expected_status
    assert summary["canonical"] is expected_accepted
    assert summary["formal_exit_status"] == (0 if expected_accepted else 2)
    assert summary["artifact_status"] == (
        "current" if expected_accepted else "current_candidate"
    )
    expected_reasons = (
        []
        if expected_accepted
        else stress_artifacts._rejection_reasons(
            candidate,
            incumbent=incumbent,
            establish_initial_baseline=False,
        )
    )
    assert candidate.get("rejection_reasons", []) == expected_reasons
    assert summary["rejection_reasons"] == expected_reasons

    expected_result = _expected_result_text(summary)
    for relative in CURRENT_DOCUMENTS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        result_block = _managed_block(
            text,
            CURRENT_RESULT_START,
            CURRENT_RESULT_END,
            path=relative,
        )
        assert result_block.strip() == expected_result, relative


def test_temporary_acceptance_and_consistency_infrastructure_is_absent() -> None:
    leftovers = sorted(
        {
            path.relative_to(ROOT).as_posix()
            for pattern in TEMPORARY_GLOBS
            for path in ROOT.glob(pattern)
        }
    )
    assert leftovers == []
