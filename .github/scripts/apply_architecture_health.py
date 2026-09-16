from pathlib import Path


def edit(path: str, transform) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    new = transform(text)
    if new == text:
        raise SystemExit(f"no change applied to {path}")
    p.write_text(new, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def evidence(text: str) -> str:
    text = replace_once(
        text,
        "from quantfusion.data.health import (\n    DataHealthIssue,\n    DataHealthReport,\n    DataHealthStatus,\n    issue_from_exception,\n)\n",
        "from quantfusion.domain.health import (\n    HealthIssue,\n    HealthReport,\n    HealthState,\n    invalid_issue,\n    issue_from_exception,\n    unavailable_issue,\n)\n",
        "evidence health import",
    )
    text = replace_once(
        text,
        "\ndef _unavailable_issue(source: str, message: str) -> DataHealthIssue:\n    return DataHealthIssue(source, DataHealthStatus.UNAVAILABLE, message)\n\n\ndef _invalid_issue(source: str, message: str) -> DataHealthIssue:\n    return DataHealthIssue(source, DataHealthStatus.INVALID, message)\n\n",
        "\n",
        "evidence old helpers",
    )
    text = text.replace("DataHealthIssue", "HealthIssue")
    text = text.replace("DataHealthReport", "HealthReport")
    text = text.replace("_unavailable_issue", "unavailable_issue")
    text = text.replace("_invalid_issue", "invalid_issue")
    text = text.replace("issue.status is DataHealthStatus.INVALID", "issue.state is HealthState.INVALID")
    return text


def governance(text: str) -> str:
    text = replace_once(
        text,
        "import numpy as np\nimport pandas as pd\n",
        "import numpy as np\nimport pandas as pd\n\nfrom quantfusion.domain.health import HealthIssue, HealthReport, HealthState\n",
        "governance import",
    )
    text = text.replace("NOT_READY_INDICATOR_RATIO", "INVALID_INDICATOR_RATIO")
    text = text.replace("NOT_READY", "INVALID")
    text = replace_once(
        text,
        "    warmup_status: str\n    required_days: int\n",
        "    health: HealthReport\n    required_days: int\n",
        "warmup health field",
    )
    text = replace_once(
        text,
        "    def as_dict(self) -> dict[str, Any]:\n        \"\"\"返回 JSON 可序列化的字典表示。\"\"\"\n        return {\n            \"warmup_status\": self.warmup_status,\n",
        "    @property\n    def warmup_status(self) -> str:\n        return self.health.state.value\n\n    def as_dict(self) -> dict[str, Any]:\n        \"\"\"返回 JSON 可序列化的字典表示。\"\"\"\n        return {\n            \"warmup_status\": self.warmup_status,\n            \"health\": self.health.as_dict(),\n",
        "warmup as_dict",
    )
    old = '''    # 失败关闭层级：regime 证据完全缺失（风险层失明）或指标就绪比例过低
    # 时判 INVALID；陈旧/缺参考成分等数据质量问题降级为 DEGRADED。
    if regime_missing or ratio < INVALID_INDICATOR_RATIO:
        status = "INVALID"
    elif reasons:
        status = "DEGRADED"
    else:
        status = "READY"

    return WarmupHealthReport(
        warmup_status=status,
'''
    new = '''    # One domain health model owns the aggregate state. Missing decision-critical
    # regime evidence or a severely cold indicator set is INVALID; incomplete
    # but still inspectable evidence is DEGRADED.
    issues: list[HealthIssue] = []
    if regime_missing:
        issues.append(
            HealthIssue(
                "warmup:regime",
                HealthState.INVALID,
                "regime_evidence_unavailable",
                "regime evidence is unavailable",
            )
        )
    if ratio < INVALID_INDICATOR_RATIO:
        issues.append(
            HealthIssue(
                "warmup:indicators",
                HealthState.INVALID,
                "indicator_history_insufficient",
                f"indicator readiness ratio {ratio:.4f} is below the usable threshold",
            )
        )
    invalid_codes = {issue.code for issue in issues}
    for reason in reasons:
        code = reason.split(":", 1)[0]
        if code in {"regime_index_missing_or_stale", "indicator_warmup_incomplete"} and invalid_codes:
            continue
        issues.append(
            HealthIssue("warmup", HealthState.DEGRADED, code, reason)
        )
    health = HealthReport.from_issues(issues)

    return WarmupHealthReport(
        health=health,
'''
    text = replace_once(text, old, new, "warmup status aggregation")
    return text


def allocation(text: str) -> str:
    text = replace_once(text, "from dataclasses import dataclass, replace\n", "from dataclasses import dataclass, field, replace\n", "allocation dataclasses")
    text = replace_once(
        text,
        "from quantfusion.domain.models import Signal\n",
        "from quantfusion.domain.health import (\n    HealthReport,\n    invalid_calculation_issue,\n)\nfrom quantfusion.domain.models import Signal\n",
        "allocation health import",
    )
    text = replace_once(
        text,
        '''    sleeve_scores: tuple[dict[str, float], ...]
    failures: tuple[AllocationScoreFailure, ...] = ()

    @property
    def status(self) -> str:
        return "degraded" if self.failures else "valid"
''',
        '''    sleeve_scores: tuple[dict[str, float], ...]
    failures: tuple[AllocationScoreFailure, ...] = ()
    health: HealthReport = field(default_factory=HealthReport)

    @property
    def status(self) -> str:
        return self.health.state.value
''',
        "allocation view health",
    )
    text = replace_once(
        text,
        "        failures: list[AllocationScoreFailure] = []\n        failed_states: list[tuple[Any, AllocationScoreFailure]] = []\n",
        "        failures: list[AllocationScoreFailure] = []\n        health_issues = []\n        failed_states: list[tuple[Any, AllocationScoreFailure]] = []\n",
        "allocation health issues init",
    )
    text = replace_once(
        text,
        "                failures.append(failure)\n                failed_states.append((state, failure))\n                sleeve_scores.append({})\n\n        view = AllocationScoreView(tuple(sleeve_scores), tuple(failures))\n",
        "                failures.append(failure)\n                health_issues.append(\n                    invalid_calculation_issue(\n                        f\"allocation:{failure.sleeve}\",\n                        f\"{failure.error_type}: {failure.message}\",\n                    )\n                )\n                failed_states.append((state, failure))\n                sleeve_scores.append({})\n\n        view = AllocationScoreView(\n            tuple(sleeve_scores),\n            tuple(failures),\n            HealthReport.from_issues(health_issues),\n        )\n",
        "allocation view construction",
    )
    return text


def daily_scan(text: str) -> str:
    text = text.replace("# NOT_READY suppresses buys. DEGRADED preserves valid risk opinions.", "# INVALID suppresses buys. DEGRADED preserves valid risk opinions.")
    text = replace_once(
        text,
        '''    warmup_not_ready = warmup_status == "NOT_READY"
    if warmup_not_ready:
        suppress_buys = True
        print("  ✗ 预热健康契约: NOT_READY — 输出不可作为正式交易信号。")
''',
        '''    warmup_invalid = warmup_status == "INVALID"
    if warmup_invalid:
        suppress_buys = True
        print("  ✗ 预热健康契约: INVALID — 输出不可作为正式交易信号。")
''',
        "daily scan invalid state",
    )
    text = text.replace('            "warmup_not_ready": warmup_not_ready,\n', "")
    return text


def daily_report(text: str) -> str:
    text = replace_once(
        text,
        '''        reasons = [description for key, description in (
            ("risk_state_identity_mismatch", "股票池或配置与上次不一致，不能确认风险状态连续性"),
            ("current_route_mismatch", "当前市场判断与历史回放结果不一致"),
            ("warmup_not_ready", "计算信号所需的历史数据不足或指数依据不可用"),
        ) if summary.get(key) is True]
        suppressed = summary.get("buys_suppressed")
''',
        '''        reasons = [description for key, description in (
            ("risk_state_identity_mismatch", "股票池或配置与上次不一致，不能确认风险状态连续性"),
            ("current_route_mismatch", "当前市场判断与历史回放结果不一致"),
        ) if summary.get(key) is True]
        if (data.get("warmup_health") or {}).get("warmup_status") == "INVALID":
            reasons.append("计算信号所需的历史数据不足或指数依据不可用")
        suppressed = summary.get("buys_suppressed")
''',
        "daily report suppression health",
    )
    text = text.replace('{"READY": "所需历史数据已就绪", "DEGRADED": "数据有缺口，须谨慎核对",\n                 "NOT_READY": "历史数据不足或必要依据不可用，本次买入已被阻止"}', '{"READY": "所需历史数据已就绪", "DEGRADED": "数据有缺口，须谨慎核对",\n                 "INVALID": "历史数据不足或必要依据不可用，本次买入已被阻止"}')
    return text


edit("quantfusion/regime/evidence.py", evidence)
edit("quantfusion/risk/governance.py", governance)
edit("quantfusion/engine/ensemble_allocation.py", allocation)
edit("quantfusion/application/daily_scan.py", daily_scan)
edit("quantfusion/application/daily_report.py", daily_report)

# Update direct consumers of the old health vocabulary. These are behavior-preserving
# test/report expectations, not compatibility aliases.
for name in [
    "tests/unit/test_data_health_and_strategy_lifecycle.py",
    "tests/unit/test_risk_governance.py",
    "tests/unit/test_production_reliability_boundaries.py",
    "tests/unit/test_daily_report.py",
    "tests/c6_non_economic/test_c6_diagnostics.py",
]:
    p = Path(name)
    text = p.read_text(encoding="utf-8")
    text = text.replace('"valid"', '"READY"') if "test_data_health" in name else text
    text = text.replace('"unavailable"', '"DEGRADED"') if "test_data_health" in name else text
    text = text.replace('"invalid"', '"INVALID"') if "test_data_health" in name else text
    text = text.replace('"status"] == "valid"', '"state"] == "READY"') if "test_data_health" in name else text
    text = text.replace('"NOT_READY"', '"INVALID"')
    text = text.replace("'NOT_READY'", "'INVALID'")
    text = text.replace('"degraded"', '"DEGRADED"') if "production_reliability" in name else text
    text = text.replace('warmup_not_ready=True', '') if "daily_report" in name else text
    p.write_text(text, encoding="utf-8")

# The domain model is now the sole health-state owner.
old_health = Path("quantfusion/data/health.py")
if not old_health.exists():
    raise SystemExit("old data health module missing")
old_health.unlink()
