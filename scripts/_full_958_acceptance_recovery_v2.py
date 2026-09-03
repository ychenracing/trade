#!/usr/bin/env python3
"""Recover, aggregate, publish, and verify the current 958-scenario acceptance."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

CURRENT_SYMBOL_COUNT = 17
CURRENT_SCENARIO_COUNT = 958
SHARD_COUNT = 12
README_START = "<!-- CURRENT_958_STRESS_START -->"
README_END = "<!-- CURRENT_958_STRESS_END -->"
VALIDATION_START = "<!-- CURRENT_958_ACCEPTANCE_START -->"
VALIDATION_END = "<!-- CURRENT_958_ACCEPTANCE_END -->"
SUMMARY_PATH = Path("artifacts/diagnostics/full-958-acceptance-summary.json")
PREFLIGHT_PATH = Path("artifacts/diagnostics/full-958-acceptance-preflight.json")
CHECKPOINT_PATH = Path("artifacts/checkpoints/full-958-stress.json")


def _formal_scenarios() -> list[dict[str, Any]]:
    from quantfusion.application import stress_scenarios
    from quantfusion.config.universe import SYMBOL_NAMES

    scenarios = stress_scenarios._multi_seed_scenarios(
        random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
        permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
        seeds=stress_scenarios.DEFAULT_SEEDS,
    )
    scenario_ids = [str(item["scenario_id"]) for item in scenarios]
    if len(SYMBOL_NAMES) != CURRENT_SYMBOL_COUNT:
        raise RuntimeError(
            f"expected {CURRENT_SYMBOL_COUNT} current symbols, got {len(SYMBOL_NAMES)}"
        )
    if len(scenarios) != CURRENT_SCENARIO_COUNT:
        raise RuntimeError(
            f"expected {CURRENT_SCENARIO_COUNT} formal scenarios, got {len(scenarios)}"
        )
    if len(set(scenario_ids)) != CURRENT_SCENARIO_COUNT:
        raise RuntimeError(
            "formal scenario plan does not contain exactly 958 unique IDs"
        )
    return scenarios


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _upsert_marked_block(
    text: str,
    start_marker: str,
    end_marker: str,
    block: str,
) -> str:
    start_count = text.count(start_marker)
    end_count = text.count(end_marker)
    if start_count == 0 and end_count == 0:
        return text.rstrip() + "\n\n" + block.strip() + "\n"
    if start_count != 1 or end_count != 1:
        raise RuntimeError(f"malformed documentation markers: {start_marker}")
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    return pattern.sub(block.strip(), text, count=1)


def _historical_validation_label(text: str) -> str:
    return text.replace(
        "当前完整 983 场景运行已保存为非 canonical 候选：",
        "历史 22 股 / 983 场景运行已保存为非 canonical 候选（不代表当前 17 股计划）：",
    )


def _write_contract_test() -> None:
    path = Path("tests/unit/test_current_formal_stress_contract.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            '''\
            """Current 17-symbol formal stress and documentation contract."""

            from pathlib import Path

            from quantfusion.application import stress_scenarios
            from quantfusion.config.universe import SYMBOL_NAMES


            def test_current_formal_plan_has_17_symbols_and_958_unique_scenarios() -> None:
                scenarios = stress_scenarios._multi_seed_scenarios(
                    random_samples=stress_scenarios.DEFAULT_RANDOM_SAMPLES,
                    permutation_samples=stress_scenarios.DEFAULT_PERMUTATION_SAMPLES,
                    seeds=stress_scenarios.DEFAULT_SEEDS,
                )
                scenario_ids = [str(item["scenario_id"]) for item in scenarios]
                assert len(SYMBOL_NAMES) == 17
                assert len(scenarios) == 958
                assert len(set(scenario_ids)) == 958


            def test_current_documentation_distinguishes_958_from_historical_983() -> None:
                readme = Path("README.md").read_text(encoding="utf-8")
                validation = Path("docs/VALIDATION.md").read_text(encoding="utf-8")
                assert "<!-- CURRENT_958_STRESS_START -->" in readme
                assert "正式计划固定为 958 个唯一场景" in readme
                assert "历史 22 股 / 983 场景" in readme
                assert "<!-- CURRENT_958_ACCEPTANCE_START -->" in validation
                assert "17 股 / 958 场景" in validation
            '''
        ),
        encoding="utf-8",
    )


def _prepare_pending_contract() -> None:
    from quantfusion.application import stress_scenarios

    scenarios = _formal_scenarios()
    scenario_ids = {str(item["scenario_id"]) for item in scenarios}

    readme_path = Path("README.md")
    readme = readme_path.read_text(encoding="utf-8")
    readme = readme.replace(
        "共 983 次生产逐日回放", "共 958 次生产逐日回放"
    ).replace(
        "全部 983 个正式场景", "全部 958 个正式场景"
    )
    if "### 股票池扩展" in readme:
        section_start = readme.index("### 股票池扩展")
        candidates = [
            index
            for index in (
                readme.find("\n### ", section_start + 4),
                readme.find("\n## ", section_start + 4),
            )
            if index >= 0
        ]
        section_end = min(candidates) if candidates else len(readme)
        section = readme[section_start:section_end].replace(
            "add-one-05-688205", "add-one-05-688072"
        )
        readme = readme[:section_start] + section + readme[section_end:]
    readme_block = f"""
{README_START}
### 当前 formal stress 合同

当前日扫与 formal stress 共用同一权威 17 股顺序。正式计划固定为 958 个唯一场景，使用 3 个固定种子、`ProductionReplayEngine`、冻结行情与原子 checkpoint；历史 22 股 / 983 场景工件仅保留作审计证据，不能代表当前计划。完整 958 场景结果正在本任务中生成，最终状态以 `docs/VALIDATION.md` 和带 provenance 的正式工件为准。
{README_END}
"""
    readme = _upsert_marked_block(
        readme, README_START, README_END, readme_block
    )
    readme_path.write_text(readme, encoding="utf-8")

    validation_path = Path("docs/VALIDATION.md")
    validation = _historical_validation_label(
        validation_path.read_text(encoding="utf-8")
    )
    validation_block = f"""
{VALIDATION_START}
## 当前 17 股 / 958 场景正式压力验收

状态：远端完整执行中。正式计划已验证为 958 个唯一场景；全部分片完成后，先合并为一个完整 checkpoint，再由生产 stress 编排器验证、计算 gates 和发布。任何 rejected 结果都保持 rejected / non-canonical，不通过修改策略经济行为、阈值、seed、场景、冻结数据或指标来制造通过。
{VALIDATION_END}
"""
    validation = _upsert_marked_block(
        validation,
        VALIDATION_START,
        VALIDATION_END,
        validation_block,
    )
    validation_path.write_text(validation, encoding="utf-8")

    _write_contract_test()
    _atomic_json(
        PREFLIGHT_PATH,
        {
            "symbol_count": CURRENT_SYMBOL_COUNT,
            "scenario_count": len(scenarios),
            "unique_scenario_ids": len(scenario_ids),
            "seeds": list(stress_scenarios.DEFAULT_SEEDS),
            "random_samples_per_size_per_seed": (
                stress_scenarios.DEFAULT_RANDOM_SAMPLES
            ),
            "permutation_samples_per_seed": (
                stress_scenarios.DEFAULT_PERMUTATION_SAMPLES
            ),
            "historical_983_evidence_preserved": True,
        },
    )
    _validate_source_comments()


def _validate_source_comments() -> None:
    stale_patterns = (
        "共 983 次生产逐日回放",
        "全部 983 个正式场景",
        "22-symbol formal stress",
        "22 股 formal stress",
        "22股 formal stress",
        "22_symbols",
    )
    ignored = {Path(__file__).name}
    hits: list[str] = []
    for root in (Path("quantfusion"), Path("scripts")):
        for path in root.rglob("*.py"):
            if path.name in ignored:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in stale_patterns:
                if pattern in text:
                    hits.append(f"{path}: {pattern}")
    if hits:
        raise RuntimeError(
            "stale current-plan source comments remain:\n" + "\n".join(hits)
        )


def _scenario_definition_matches(
    result: dict[str, Any], definition: dict[str, Any]
) -> bool:
    return all(result.get(key) == value for key, value in definition.items())


def _validate_shard_payload(
    payload: dict[str, Any],
    *,
    source_sha: str,
    shard_index: int,
) -> list[dict[str, Any]]:
    scenarios = _formal_scenarios()
    expected = [
        scenario
        for index, scenario in enumerate(scenarios)
        if index % SHARD_COUNT == shard_index
    ]
    expected_by_id = {str(item["scenario_id"]): item for item in expected}
    if payload.get("source_revision") != source_sha:
        raise ValueError("shard source revision mismatch")
    if int(payload.get("shard_index", -1)) != shard_index:
        raise ValueError("shard index mismatch")
    if int(payload.get("shard_count", -1)) != SHARD_COUNT:
        raise ValueError("shard count mismatch")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("shard results must be a list")
    if len(results) != len(expected):
        raise ValueError(
            f"shard result count mismatch: expected {len(expected)}, got {len(results)}"
        )
    ids = [str(item.get("scenario_id")) for item in results]
    if len(set(ids)) != len(ids) or set(ids) != set(expected_by_id):
        raise ValueError("shard scenario IDs are incomplete or duplicated")
    for result in results:
        scenario_id = str(result["scenario_id"])
        if not _scenario_definition_matches(result, expected_by_id[scenario_id]):
            raise ValueError(f"scenario definition mismatch: {scenario_id}")
    return results


def _find_existing_shard(path: Path) -> Path | None:
    if path.is_file():
        return path
    if path.is_dir():
        matches = sorted(path.rglob("shard-results.json"))
        return matches[0] if matches else None
    return None


def _run_or_reuse_shard(args: argparse.Namespace) -> None:
    from quantfusion.application import stress
    from quantfusion.config.paths import REGIME_DATA_DIR

    source_sha = str(args.source_sha)
    shard_index = int(args.shard_index)
    output = Path(args.output)
    existing = _find_existing_shard(Path(args.existing)) if args.existing else None
    if existing is not None:
        try:
            payload = json.loads(existing.read_text(encoding="utf-8"))
            _validate_shard_payload(
                payload,
                source_sha=source_sha,
                shard_index=shard_index,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(existing, output)
            print(f"reused validated shard {shard_index}")
            return

    scenarios = _formal_scenarios()
    selected = [
        scenario
        for index, scenario in enumerate(scenarios)
        if index % SHARD_COUNT == shard_index
    ]
    completed: list[dict[str, Any]] = []
    worker = partial(
        stress._run_scenario,
        data_dir=stress.DATA_DIR,
        regime_data_dir=REGIME_DATA_DIR,
        include_diagnostics=False,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=int(args.workers)) as executor:
        for result in executor.map(worker, selected):
            completed.append(result)
            if len(completed) % 5 == 0:
                _atomic_json(
                    output,
                    {
                        "source_revision": source_sha,
                        "shard_index": shard_index,
                        "shard_count": SHARD_COUNT,
                        "scenario_count": len(selected),
                        "results": completed,
                    },
                )
    payload = {
        "source_revision": source_sha,
        "shard_index": shard_index,
        "shard_count": SHARD_COUNT,
        "scenario_count": len(selected),
        "results": completed,
    }
    _validate_shard_payload(
        payload,
        source_sha=source_sha,
        shard_index=shard_index,
    )
    _atomic_json(output, payload)
    print(f"completed shard {shard_index}/{SHARD_COUNT}: {len(completed)} scenarios")


def _select_initial_baseline_reference(
    expected_ids: set[str],
    contract_version: object,
) -> tuple[Path, dict[str, Any]]:
    from quantfusion.application import stress_artifacts

    candidates: list[tuple[int, int, str, Path, dict[str, Any]]] = []
    for path in stress_artifacts.VALIDATION_ARTIFACT_DIR.rglob("*.json"):
        try:
            payload = stress_artifacts._load_initial_baseline_reference(path)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            continue
        ids = {
            str(item.get("scenario_id"))
            for item in results
            if isinstance(item, dict) and item.get("scenario_id") is not None
        }
        shared = len(ids & expected_ids)
        if shared == 0:
            continue
        same_contract = int(
            payload.get("stress_contract_version") == contract_version
        )
        candidates.append(
            (same_contract, shared, path.as_posix(), path, payload)
        )
    if not candidates:
        raise RuntimeError(
            "no validated retained current-semantic baseline reference exists"
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    same_contract, shared, _, path, payload = candidates[-1]
    return path, {
        "mode": "establish_initial_baseline",
        "reference": path.as_posix(),
        "shared_scenario_ids": shared,
        "same_contract_version": bool(same_contract),
        "reference_source_revision": payload.get("source_revision"),
    }


def _locate_formal_artifact(source_sha: str) -> tuple[Path, dict[str, Any]]:
    from quantfusion.application import stress_artifacts

    candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in stress_artifacts.VALIDATION_ARTIFACT_DIR.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        results = payload.get("results")
        if (
            payload.get("source_revision") != source_sha
            or payload.get("scenario_count") != CURRENT_SCENARIO_COUNT
            or not isinstance(results, list)
            or len(results) != CURRENT_SCENARIO_COUNT
        ):
            continue
        priority = 0
        if payload.get("acceptance_status") == "rejected":
            priority = 1
        if payload.get("canonical") is True:
            priority = 2
        candidates.append((priority, path.as_posix(), path, payload))
    if not candidates:
        raise RuntimeError(
            "production stress orchestrator did not publish an exact 958 result artifact"
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    _, _, path, payload = candidates[-1]
    return path, payload


def _sync_actual_documentation(
    summary: dict[str, Any], artifact: dict[str, Any]
) -> None:
    source_sha = str(summary["source_revision"])
    artifact_path = str(summary["artifact_path"])
    canonical = bool(summary["canonical"])
    status = str(summary["acceptance_status"])
    exit_code = int(summary["orchestrator_exit_code"])
    mode = summary.get("baseline_mode", {})
    status_text = "通过并成为 canonical" if canonical else "未通过，保持 non-canonical"

    readme_path = Path("README.md")
    readme = readme_path.read_text(encoding="utf-8")
    readme = readme.replace(
        "共 983 次生产逐日回放", "共 958 次生产逐日回放"
    ).replace(
        "全部 983 个正式场景", "全部 958 个正式场景"
    )
    readme_block = f"""
{README_START}
### 当前 formal stress 合同

当前日扫与 formal stress 共用同一权威 17 股顺序。正式计划固定为 958 个唯一场景，使用 3 个固定种子、`ProductionReplayEngine`、冻结行情与原子 checkpoint。最新完整验收结果：**{status_text}**；source revision 为 `{source_sha}`。历史 22 股 / 983 场景工件仅保留作审计证据，不能代表当前计划。详细 gates、provenance 和指标见 `docs/VALIDATION.md` 及 `{artifact_path}`。
{README_END}
"""
    readme = _upsert_marked_block(
        readme, README_START, README_END, readme_block
    )
    readme_path.write_text(readme, encoding="utf-8")

    gate_fields = (
        "absolute_hard_gates",
        "retained_robustness_hard_gates",
        "promotion_gates",
        "initial_baseline_gates",
    )
    gate_lines = [
        "| Gate family | passed | status |",
        "|---|---:|---|",
    ]
    checks_payload: dict[str, object] = {}
    for name in gate_fields:
        gate = artifact.get(name)
        if not isinstance(gate, dict):
            continue
        gate_lines.append(
            f"| `{name}` | `{gate.get('passed')}` | `{gate.get('status', '')}` |"
        )
        checks_payload[name] = gate.get("checks", {})
    all_summary = (
        artifact.get("summary", {}).get("all", {})
        if isinstance(artifact.get("summary"), dict)
        else {}
    )
    validation_block = f"""
{VALIDATION_START}
## 当前 17 股 / 958 场景正式压力验收

- 结果：**{status_text}**
- `acceptance_status`: `{status}`
- `canonical`: `{str(canonical).lower()}`
- 生产编排器退出码：`{exit_code}`（`0` 表示 accepted，`2` 表示 truthful rejected）
- 场景：`958 / 958`，唯一 ID：`958`
- source revision：`{source_sha}`
- stress contract：`{artifact.get('stress_contract_version')}`
- source fingerprint：`{artifact.get('source_fingerprint')}`
- data fingerprint：`{artifact.get('data_fingerprint')}`
- scenario signature：`{artifact.get('scenario_signature')}`
- run signature：`{artifact.get('run_signature')}`
- 正式工件：`{artifact_path}`
- baseline mode：`{mode.get('mode')}`
- baseline reference：`{mode.get('reference')}`

{chr(10).join(gate_lines)}

Gate checks：

```json
{json.dumps(checks_payload, ensure_ascii=False, indent=2, allow_nan=False)}
```

全计划汇总指标：

```json
{json.dumps(all_summary, ensure_ascii=False, indent=2, allow_nan=False)}
```

本次执行没有为了获得绿色结果而修改策略经济行为、风险阈值、seed、场景、冻结数据或指标。历史 22 股 / 983 场景工件继续原样保留，仅用于历史审计，不作为当前 17 股 / 958 场景结论。
{VALIDATION_END}
"""
    validation_path = Path("docs/VALIDATION.md")
    validation = _historical_validation_label(
        validation_path.read_text(encoding="utf-8")
    )
    validation = _upsert_marked_block(
        validation,
        VALIDATION_START,
        VALIDATION_END,
        validation_block,
    )
    validation_path.write_text(validation, encoding="utf-8")
    _write_contract_test()


def _aggregate_and_publish(args: argparse.Namespace) -> None:
    from quantfusion.application import (
        stress,
        stress_artifacts,
        stress_scenarios,
    )
    from quantfusion.config.paths import REGIME_DATA_DIR

    source_sha = str(args.source_sha)
    scenarios = _formal_scenarios()
    expected = {str(item["scenario_id"]): item for item in scenarios}
    merged: dict[str, dict[str, Any]] = {}
    seen_shards: set[int] = set()
    shard_paths = sorted(Path(args.shard_root).rglob("shard-results.json"))
    if len(shard_paths) != SHARD_COUNT:
        raise RuntimeError(
            f"expected {SHARD_COUNT} shard files, got {len(shard_paths)}"
        )
    for path in shard_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        shard_index = int(payload.get("shard_index", -1))
        if shard_index in seen_shards:
            raise RuntimeError(f"duplicate shard index {shard_index}")
        seen_shards.add(shard_index)
        results = _validate_shard_payload(
            payload,
            source_sha=source_sha,
            shard_index=shard_index,
        )
        for result in results:
            scenario_id = str(result["scenario_id"])
            if scenario_id in merged:
                raise RuntimeError(f"duplicate scenario result {scenario_id}")
            if scenario_id not in expected:
                raise RuntimeError(f"unexpected scenario result {scenario_id}")
            merged[scenario_id] = result
    if seen_shards != set(range(SHARD_COUNT)):
        raise RuntimeError(f"incomplete shard indices: {sorted(seen_shards)}")
    if set(merged) != set(expected):
        missing = sorted(set(expected) - set(merged))
        extra = sorted(set(merged) - set(expected))
        raise RuntimeError(
            f"full result set mismatch missing={missing} extra={extra}"
        )

    provenance = stress_artifacts._build_provenance(
        scenarios,
        Path(stress.DATA_DIR),
        Path(REGIME_DATA_DIR),
        source_revision=source_sha,
    )
    checkpoint = {
        "signature": provenance["run_signature"],
        "provenance": provenance,
        "completed": CURRENT_SCENARIO_COUNT,
        "scenario_count": CURRENT_SCENARIO_COUNT,
        "results": sorted(
            merged.values(), key=lambda item: str(item["scenario_id"])
        ),
    }
    _atomic_json(CHECKPOINT_PATH, checkpoint)
    validated = stress_artifacts._validated_checkpoint(
        checkpoint,
        scenarios,
        signature=str(provenance["run_signature"]),
        provenance=provenance,
    )
    if len(validated) != CURRENT_SCENARIO_COUNT:
        raise RuntimeError(
            "production checkpoint validator did not accept all 958 results"
        )

    incumbent_path = stress_artifacts.VALIDATION_ARTIFACT_DIR / "universe_stress.json"
    incumbent = stress_artifacts._load_incumbent(incumbent_path)
    command = [
        sys.executable,
        "-m",
        "quantfusion.application.stress",
        "--workers",
        "1",
        "--checkpoint",
        str(CHECKPOINT_PATH),
        "--source-revision",
        source_sha,
    ]
    if incumbent is not None:
        baseline_mode: dict[str, Any] = {
            "mode": "promotion_against_incumbent",
            "reference": incumbent_path.as_posix(),
            "incumbent_source_revision": incumbent.get("source_revision"),
        }
    else:
        reference_path, baseline_mode = _select_initial_baseline_reference(
            set(expected), provenance.get("stress_contract_version")
        )
        command.extend(
            [
                "--establish-initial-baseline",
                "--initial-baseline-reference",
                reference_path.as_posix(),
            ]
        )

    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    Path("full-958-stress-output.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    print(completed.stdout, end="")
    if completed.returncode not in {0, 2}:
        raise RuntimeError(
            "production stress orchestrator failed unexpectedly with "
            f"exit code {completed.returncode}"
        )

    artifact_path, artifact = _locate_formal_artifact(source_sha)
    status = str(artifact.get("acceptance_status", "unknown"))
    canonical = bool(artifact.get("canonical", False))
    if completed.returncode == 0 and (status != "accepted" or not canonical):
        raise RuntimeError(
            "exit 0 did not produce an accepted canonical artifact"
        )
    if completed.returncode == 2 and (status != "rejected" or canonical):
        raise RuntimeError(
            "exit 2 did not preserve rejected non-canonical semantics"
        )
    actual_ids = {
        str(item["scenario_id"]) for item in artifact.get("results", [])
    }
    if actual_ids != set(expected):
        raise RuntimeError(
            "published artifact does not contain the exact 958-scenario formal plan"
        )

    gate_fields = (
        "absolute_hard_gates",
        "retained_robustness_hard_gates",
        "promotion_gates",
        "initial_baseline_gates",
    )
    gates = {
        name: artifact.get(name)
        for name in gate_fields
        if artifact.get(name) is not None
    }
    summary: dict[str, Any] = {
        "acceptance_status": status,
        "canonical": canonical,
        "orchestrator_exit_code": completed.returncode,
        "artifact_path": artifact_path.as_posix(),
        "source_revision": source_sha,
        "stress_contract_version": artifact.get("stress_contract_version"),
        "source_fingerprint": artifact.get("source_fingerprint"),
        "data_fingerprint": artifact.get("data_fingerprint"),
        "scenario_signature": artifact.get("scenario_signature"),
        "run_signature": artifact.get("run_signature"),
        "scenario_count": CURRENT_SCENARIO_COUNT,
        "unique_scenario_ids": len(actual_ids),
        "baseline_mode": baseline_mode,
        "gates": gates,
        "summary": artifact.get("summary"),
    }
    _atomic_json(SUMMARY_PATH, summary)
    _sync_actual_documentation(summary, artifact)
    _verify_summary_and_docs()


def _load_valid_summary() -> tuple[dict[str, Any], dict[str, Any]]:
    if not SUMMARY_PATH.is_file():
        raise FileNotFoundError(SUMMARY_PATH)
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    artifact_path = Path(str(summary["artifact_path"]))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    scenarios = _formal_scenarios()
    expected = {str(item["scenario_id"]) for item in scenarios}
    results = artifact.get("results")
    if not isinstance(results, list):
        raise ValueError("formal artifact results must be a list")
    actual = {str(item["scenario_id"]) for item in results}
    if artifact.get("scenario_count") != CURRENT_SCENARIO_COUNT:
        raise ValueError("formal artifact scenario_count is not 958")
    if len(results) != CURRENT_SCENARIO_COUNT or actual != expected:
        raise ValueError("formal artifact does not contain the exact 958 plan")
    if artifact.get("source_revision") != summary.get("source_revision"):
        raise ValueError("summary and formal artifact source revisions differ")
    if summary.get("scenario_count") != CURRENT_SCENARIO_COUNT:
        raise ValueError("acceptance summary scenario_count is not 958")
    if summary.get("unique_scenario_ids") != CURRENT_SCENARIO_COUNT:
        raise ValueError("acceptance summary unique scenario count is not 958")
    status = summary.get("acceptance_status")
    canonical = summary.get("canonical")
    exit_code = summary.get("orchestrator_exit_code")
    if status == "accepted":
        if canonical is not True or exit_code != 0:
            raise ValueError("accepted result lacks canonical/exit-0 semantics")
    elif status == "rejected":
        if canonical is not False or exit_code != 2:
            raise ValueError("rejected result lacks non-canonical/exit-2 semantics")
    else:
        raise ValueError(f"unexpected acceptance status {status!r}")
    for field in (
        "source_fingerprint",
        "data_fingerprint",
        "scenario_signature",
        "run_signature",
    ):
        value = summary.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"acceptance summary lacks {field}")
    return summary, artifact


def _verify_summary_and_docs() -> None:
    summary, artifact = _load_valid_summary()
    _sync_actual_documentation(summary, artifact)
    readme = Path("README.md").read_text(encoding="utf-8")
    validation = Path("docs/VALIDATION.md").read_text(encoding="utf-8")
    required_readme = (
        README_START,
        "正式计划固定为 958 个唯一场景",
        "历史 22 股 / 983 场景",
        str(summary["source_revision"]),
    )
    required_validation = (
        VALIDATION_START,
        "17 股 / 958 场景",
        "场景：`958 / 958`",
        "历史 22 股 / 983 场景",
        str(summary["artifact_path"]),
    )
    for value in required_readme:
        if value not in readme:
            raise RuntimeError(f"README is missing current contract text: {value}")
    for value in required_validation:
        if value not in validation:
            raise RuntimeError(
                f"VALIDATION.md is missing current acceptance text: {value}"
            )
    for stale in (
        "共 983 次生产逐日回放",
        "全部 983 个正式场景",
    ):
        if stale in readme:
            raise RuntimeError(f"README still presents stale current count: {stale}")
    _validate_source_comments()
    if not Path("tests/unit/test_current_formal_stress_contract.py").is_file():
        raise RuntimeError("current formal stress contract test is missing")
    if not PREFLIGHT_PATH.is_file():
        raise RuntimeError("current formal stress preflight artifact is missing")


def _inspect(args: argparse.Namespace) -> None:
    output_path = Path(args.github_output) if args.github_output else None
    try:
        summary, _ = _load_valid_summary()
    except (
        FileNotFoundError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        values = {
            "needs_recovery": "true",
            "summary_source_sha": "",
            "inspection": f"missing_or_invalid:{type(exc).__name__}",
        }
    else:
        values = {
            "needs_recovery": "false",
            "summary_source_sha": str(summary["source_revision"]),
            "inspection": "valid_complete_summary",
        }
    if output_path is not None:
        with output_path.open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))


def _fingerprint_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report() -> None:
    summary, artifact = _load_valid_summary()
    report = {
        "summary": summary,
        "formal_artifact_sha256": _fingerprint_file(
            Path(str(summary["artifact_path"]))
        ),
        "summary_sha256": _fingerprint_file(SUMMARY_PATH),
        "formal_result_count": len(artifact["results"]),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("prepare")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--github-output")

    shard_parser = subparsers.add_parser("run-or-reuse-shard")
    shard_parser.add_argument("--source-sha", required=True)
    shard_parser.add_argument("--shard-index", type=int, required=True)
    shard_parser.add_argument("--existing")
    shard_parser.add_argument("--output", required=True)
    shard_parser.add_argument("--workers", type=int, default=2)

    aggregate_parser = subparsers.add_parser("aggregate")
    aggregate_parser.add_argument("--source-sha", required=True)
    aggregate_parser.add_argument("--shard-root", required=True)

    subparsers.add_parser("verify")
    subparsers.add_parser("report")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "prepare":
        _prepare_pending_contract()
    elif args.command == "inspect":
        _inspect(args)
    elif args.command == "run-or-reuse-shard":
        _run_or_reuse_shard(args)
    elif args.command == "aggregate":
        _aggregate_and_publish(args)
    elif args.command == "verify":
        _verify_summary_and_docs()
    elif args.command == "report":
        _report()
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
