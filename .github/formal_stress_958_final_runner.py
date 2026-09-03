#!/usr/bin/env python3
"""Branch-only orchestration helpers for the exact current formal acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
OLD_REJECTED_SHA256 = "adda276bea8a11b76fa6881e4e7a9770bf8cfb79bb93a397aa5aa405327358c2"
START = "<!-- CURRENT_FORMAL_STRESS_958_START -->"
END = "<!-- CURRENT_FORMAL_STRESS_958_END -->"
DOCS = (Path("README.md"), Path("docs/VALIDATION.md"), Path("docs/ARCHITECTURE.md"))


def _runtime_contract() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from quantfusion.application import stress_scenarios
    from quantfusion.config import daily
    from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES

    if tuple(SYMBOL_NAMES) != EXPECTED_SYMBOLS:
        raise SystemExit(f"formal universe is not the ordered 17-symbol contract: {tuple(SYMBOL_NAMES)!r}")
    if tuple(daily.SYMBOLS) != EXPECTED_SYMBOLS:
        raise SystemExit(f"daily universe is not the ordered 17-symbol contract: {tuple(daily.SYMBOLS)!r}")
    expected_validation = {
        "1_symbol": EXPECTED_SYMBOLS[:1],
        "3_symbols": EXPECTED_SYMBOLS[:3],
        "5_symbols": EXPECTED_SYMBOLS[:5],
        "13_symbols": EXPECTED_SYMBOLS[:13],
        "17_symbols": EXPECTED_SYMBOLS,
    }
    if VALIDATION_UNIVERSES != expected_validation:
        raise SystemExit(f"validation universes differ: {VALIDATION_UNIVERSES!r}")
    scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )
    ids = [str(item["scenario_id"]) for item in scenarios]
    families = Counter(str(item["scenario_type"]) for item in scenarios)
    if len(scenarios) != 958 or len(set(ids)) != 958:
        raise SystemExit(
            f"formal plan must contain 958 unique IDs; count={len(scenarios)}, unique={len(set(ids))}"
        )
    if dict(families) != EXPECTED_FAMILIES:
        raise SystemExit(f"formal family counts differ: {dict(families)!r}")
    return scenarios, {
        "symbols": list(EXPECTED_SYMBOLS),
        "scenario_count": 958,
        "unique_scenario_ids": 958,
        "family_counts": dict(families),
    }


def _replace_block(path: Path, body: str) -> None:
    text = path.read_text(encoding="utf-8")
    rendered = f"{START}\n{body.rstrip()}\n{END}"
    if START in text or END in text:
        if text.count(START) != 1 or text.count(END) != 1:
            raise SystemExit(f"malformed current-result marker in {path}")
        before, remainder = text.split(START, 1)
        _, after = remainder.split(END, 1)
        text = before.rstrip() + "\n\n" + rendered + after
    else:
        text = text.rstrip() + "\n\n" + rendered + "\n"
    path.write_text(text, encoding="utf-8")


def _normalize_current_docs() -> None:
    replacements = {
        "共 983 次生产逐日回放": "共 958 次生产逐日回放",
        "共983次生产逐日回放": "共958次生产逐日回放",
        "全部 983 个正式场景": "全部 958 个正式场景",
        "全部983个正式场景": "全部958个正式场景",
        "--scenario-id add-one-05-688205": "--scenario-id add-one-05-688072",
        "1 / 3 / 5 / 13 / 22": "1 / 3 / 5 / 13 / 17",
        "1/3/5/13/22": "1/3/5/13/17",
        "1、3、5、13、22": "1、3、5、13、17",
    }
    historical_replacements = {
        "当前完整 983 场景运行已保存为非 canonical 候选": (
            "历史22股完整983场景运行保留为非 canonical 候选（不代表当前17股计划）"
        ),
        "当前完整983场景运行已保存为非 canonical 候选": (
            "历史22股完整983场景运行保留为非 canonical 候选（不代表当前17股计划）"
        ),
    }
    for relative in DOCS:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        for old, new in historical_replacements.items():
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")


def _write_contract_test() -> None:
    path = ROOT / "tests/unit/test_current_formal_stress_contract.py"
    path.write_text(
        '''"""Current ordered 17-symbol and exact 958-scenario formal contract."""\n\n'
        'from __future__ import annotations\n\n'
        'import hashlib\n'
        'import json\n'
        'from collections import Counter\n'
        'from pathlib import Path\n\n'
        'from quantfusion.application import stress_scenarios\n'
        'from quantfusion.config import daily\n'
        'from quantfusion.config.universe import SYMBOL_NAMES, VALIDATION_UNIVERSES\n\n'
        'EXPECTED = (\n'
        '    "300308", "300502", "300394", "688256", "603986",\n'
        '    "688072", "688300", "300054", "688361", "002409",\n'
        '    "688498", "688120", "002384", "688082", "300604",\n'
        '    "601869", "300408",\n'
        ')\n\n'
        'def test_current_trade_and_formal_universe_are_identical() -> None:\n'
        '    assert tuple(SYMBOL_NAMES) == EXPECTED\n'
        '    assert tuple(daily.SYMBOLS) == EXPECTED\n'
        '    assert VALIDATION_UNIVERSES["17_symbols"] == EXPECTED\n\n'
        'def test_current_formal_plan_is_exactly_958_unique_scenarios() -> None:\n'
        '    scenarios = stress_scenarios._multi_seed_scenarios(\n'
        '        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,\n'
        '        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,\n'
        '        seeds=stress_scenarios.DEFAULT_SEEDS,\n'
        '    )\n'
        '    assert len(scenarios) == 958\n'
        '    assert len({item["scenario_id"] for item in scenarios}) == 958\n'
        '    assert Counter(item["scenario_type"] for item in scenarios) == {\n'
        '        "prefix": 17,\n'
        '        "leave_one_out": 17,\n'
        '        "add_one": 24,\n'
        '        "random_subset": 750,\n'
        '        "permutation": 150,\n'
        '    }\n\n'
        'def test_persisted_958_acceptance_report_is_self_consistent_when_present() -> None:\n'
        '    path = Path("artifacts/validation/formal_stress_958_acceptance.json")\n'
        '    if not path.is_file():\n'
        '        return\n'
        '    report = json.loads(path.read_text(encoding="utf-8"))\n'
        '    assert report["status"] == "completed"\n'
        '    assert report["scenario_count"] == 958\n'
        '    assert report["unique_scenario_ids"] == 958\n'
        '    artifact = Path(report["formal_artifact"])\n'
        '    assert artifact.is_file()\n'
        '    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == report["formal_artifact_sha256"]\n'
        '    historical = Path(report["historical_reference"])\n'
        '    assert hashlib.sha256(historical.read_bytes()).hexdigest() == report["historical_reference_sha256"]\n''',
        encoding="utf-8",
    )


def prepare() -> None:
    _, contract = _runtime_contract()
    _normalize_current_docs()
    _write_contract_test()
    block = (
        "## 当前17股、958场景正式压力计划\n\n"
        "日扫与 formal stress 使用同一有序17股集合。当前正式计划固定为 "
        "958 个唯一场景：17个 prefix、17个 leave-one-out、24个 add-one、"
        "750个 random-subset 和150个 permutation。\n\n"
        "精确全量回放尚在执行；完成前没有当前17股 accepted canonical。"
        "历史22股/983场景 rejected 工件只作为不可变历史证据。"
    )
    for relative in DOCS:
        _replace_block(ROOT / relative, block)
    preflight = {
        "status": "prepared",
        **contract,
        "historical_22_symbol_983_artifact_retained": True,
        "no_economic_or_threshold_change": True,
    }
    path = ROOT / "artifacts/diagnostics/formal-stress-958-preflight.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def find_historical_reference() -> Path:
    matches: list[Path] = []
    for path in sorted((ROOT / "artifacts/validation/candidates").glob("stress-*-rejected.json")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest == OLD_REJECTED_SHA256:
            matches.append(path)
    if len(matches) != 1:
        raise SystemExit(
            f"expected one historical rejected artifact with {OLD_REJECTED_SHA256}; found {matches!r}"
        )
    return matches[0]


def _finite(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise SystemExit(f"{label} is non-finite: {value!r}")
    return number


def _find_current_artifact(source_revision: str) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((ROOT / "artifacts/validation").rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            str(payload.get("source_revision", "")) == source_revision
            and int(payload.get("scenario_count", -1)) == 958
            and isinstance(payload.get("results"), list)
            and len(payload["results"]) == 958
            and "absolute_hard_gates" in payload
            and "retained_robustness_hard_gates" in payload
        ):
            matches.append((path, payload))
    if not matches:
        raise SystemExit(f"no exact 958 formal artifact for source revision {source_revision}")
    matches.sort(key=lambda item: (not bool(item[1].get("canonical", False)), str(item[0])))
    return matches[0]


def summarize(source_revision: str, product_exit_code: int) -> None:
    _, contract = _runtime_contract()
    artifact_path, payload = _find_current_artifact(source_revision)
    results = payload["results"]
    scenario_ids = [str(item["scenario_id"]) for item in results]
    families = Counter(str(item["scenario_type"]) for item in results)
    if len(set(scenario_ids)) != 958 or dict(families) != EXPECTED_FAMILIES:
        raise SystemExit("published formal artifact does not match the exact 958 plan")
    for index, item in enumerate(results):
        for key in ("total_return", "max_drawdown", "sharpe", "calmar"):
            _finite(item[key], f"results[{index}].{key}")
    worst = min(results, key=lambda item: _finite(item["max_drawdown"], "max_drawdown"))
    historical = find_historical_reference()
    historical_sha = hashlib.sha256(historical.read_bytes()).hexdigest()
    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    absolute = payload.get("absolute_hard_gates", {})
    retained = payload.get("retained_robustness_hard_gates", {})
    robustness = payload.get("robustness_diagnostics", {})
    promotion = payload.get("promotion_gates", {})
    initial = payload.get("initial_baseline_gates", {})
    report = {
        "status": "completed",
        "source_revision": source_revision,
        "product_exit_code": product_exit_code,
        "acceptance_status": str(payload.get("acceptance_status", "unknown")),
        "canonical": bool(payload.get("canonical", False)),
        **contract,
        "formal_artifact": str(artifact_path.relative_to(ROOT)),
        "formal_artifact_sha256": artifact_sha,
        "historical_reference": str(historical.relative_to(ROOT)),
        "historical_reference_sha256": historical_sha,
        "summary": payload.get("summary", {}),
        "absolute_hard_gates": absolute,
        "retained_robustness_hard_gates": retained,
        "robustness_diagnostics": robustness,
        "promotion_gates": promotion,
        "initial_baseline_gates": initial,
        "worst_scenario": {
            "scenario_id": str(worst["scenario_id"]),
            "scenario_type": str(worst["scenario_type"]),
            "symbol_count": int(worst["symbol_count"]),
            "symbols": list(worst["symbols"]),
            "total_return": _finite(worst["total_return"], "worst.total_return"),
            "max_drawdown": _finite(worst["max_drawdown"], "worst.max_drawdown"),
            "sharpe": _finite(worst["sharpe"], "worst.sharpe"),
            "calmar": _finite(worst["calmar"], "worst.calmar"),
        },
    }
    report_path = ROOT / "artifacts/validation/formal_stress_958_acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    accepted = report["acceptance_status"] == "accepted"
    block = (
        "## 当前17股、958场景正式压力验收\n\n"
        f"- 回放 source revision：`{source_revision}`\n"
        "- 完整性：`958/958`；唯一 scenario ID：`958`\n"
        "- 场景族：prefix 17、leave-one-out 17、add-one 24、"
        "random-subset 750、permutation 150\n"
        f"- Formal status：`{report['acceptance_status']}`\n"
        f"- Canonical：`{str(report['canonical']).lower()}`\n"
        f"- 18% absolute hard gates：`{'passed' if absolute.get('passed') else 'failed'}`\n"
        f"- Retained robustness hard gates：`{'passed' if retained.get('passed') else 'failed'}`\n"
        f"- 全场景最差最大回撤：`{abs(float(worst['max_drawdown'])):.12%}`\n"
        f"- 最差场景：`{worst['scenario_id']}`（{worst['scenario_type']}，"
        f"{worst['symbol_count']}只）\n"
        f"- 工件：`{report['formal_artifact']}`\n"
        f"- 工件 SHA-256：`{artifact_sha}`\n"
        f"- 历史22股/983场景 rejected 工件 SHA-256：`{historical_sha}`（保持不变）\n\n"
        "该结论仅适用于记录的冻结行情、17股顺序、生产逐日回放以及 source/data/"
        "scenario/run fingerprints。日线信号在收盘形成并于下一可交易开盘执行；"
        "这不是未来收益或实盘最大回撤保证。"
    )
    for relative in DOCS:
        _replace_block(ROOT / relative, block)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if accepted and not report["canonical"]:
        raise SystemExit("accepted formal result must be canonical")
    if not accepted and report["canonical"]:
        raise SystemExit("rejected formal result must not be canonical")
    if product_exit_code not in (0, 2):
        raise SystemExit(f"invalid formal product exit code: {product_exit_code}")
    if (product_exit_code == 0) != accepted:
        raise SystemExit(
            f"formal exit/status mismatch: exit={product_exit_code}, status={report['acceptance_status']}"
        )


def verify_report() -> None:
    path = ROOT / "artifacts/validation/formal_stress_958_acceptance.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("status") != "completed":
        raise SystemExit("formal report is not complete")
    if report.get("scenario_count") != 958 or report.get("unique_scenario_ids") != 958:
        raise SystemExit("formal report is not exact 958/958")
    artifact = ROOT / str(report["formal_artifact"])
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != report["formal_artifact_sha256"]:
        raise SystemExit("formal artifact SHA mismatch")
    historical = ROOT / str(report["historical_reference"])
    if hashlib.sha256(historical.read_bytes()).hexdigest() != report["historical_reference_sha256"]:
        raise SystemExit("historical artifact SHA mismatch")
    for relative in DOCS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        if text.count(START) != 1 or text.count(END) != 1:
            raise SystemExit(f"generated block mismatch in {relative}")
        if report["source_revision"] not in text or report["formal_artifact_sha256"] not in text:
            raise SystemExit(f"result identity missing from {relative}")
        if "958/958" not in text:
            raise SystemExit(f"completion missing from {relative}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "reference", "summarize", "verify"))
    parser.add_argument("--source-revision")
    parser.add_argument("--product-exit-code", type=int, default=0)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    elif args.mode == "reference":
        print(find_historical_reference().relative_to(ROOT))
    elif args.mode == "summarize":
        if not args.source_revision:
            raise SystemExit("--source-revision is required")
        summarize(args.source_revision, args.product_exit_code)
    else:
        verify_report()


if __name__ == "__main__":
    main()
