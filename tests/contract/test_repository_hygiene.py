"""仓库结构、中文文档和文档—代码一致性的回归测试。"""

from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy
from quantfusion.config.paths import PROJECT_ROOT
from quantfusion.config import paths as repository_paths
from scripts.run_regime_validation import _golden_bull
from scripts.backtest_universes import UNIVERSES


ROOT = PROJECT_ROOT
THIS_FILE = Path(__file__).resolve().relative_to(ROOT)
EXPECTED_MARKDOWN = {
    Path(".github/CHATGPT_PROJECT_BRIEF.md"),
    Path(".github/pull_request_template.md"),
    Path("AGENTS.md"),
    Path("README.md"),
    Path("docs/ARCHITECTURE.md"),
    Path("docs/VALIDATION.md"),
    Path("data/README.md"),
}
CHINESE_MARKDOWN = {
    Path("README.md"),
    Path("docs/ARCHITECTURE.md"),
    Path("docs/VALIDATION.md"),
    Path("data/README.md"),
}
EXPECTED_ROOT_FILES = {
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "pyrightconfig.json",
    "requirements-dev.txt",
    "requirements-lock-py311.txt",
    "requirements-lock.txt",
    "requirements.txt",
}
OBSOLETE_DOCUMENTS = {
    "BACKTEST_RESULTS.md",
    "STRATEGY_REVIEW.md",
    "PRODUCTION_REVIEW_FIXES.md",
    "REGIME_ADAPTIVE_REFACTOR_REPORT.md",
    "TRANSFORMATION_REPORT.md",
}
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _markdown_prose_lines(content: str) -> list[str]:
    """返回代码围栏之外的非空 Markdown 行。"""
    lines: list[str] = []
    in_fence = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        is_html_comment = (
            stripped.startswith("<!--") and stripped.endswith("-->")
        )
        if not in_fence and stripped and not is_html_comment:
            lines.append(stripped)
    return lines


def _tracked_paths() -> tuple[Path, ...]:
    """读取 Git 索引，而不是把测试运行产生的缓存误认为已提交文件。"""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    )
    paths = (
        Path(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
    )
    return tuple(path for path in paths if (ROOT / path).exists())


class MarkdownConsistencyTests(unittest.TestCase):
    """保证仓库只保留当前有效中文文档。"""

    def test_only_current_markdown_documents_are_kept(self) -> None:
        found = {path for path in _tracked_paths() if path.suffix == ".md"}
        self.assertEqual(found, EXPECTED_MARKDOWN)

    def test_markdown_metadata_comments_are_not_prose(self) -> None:
        lines = _markdown_prose_lines(
            "<!-- CURRENT_FORMAL_STRESS_PLAN:START -->\n"
            "当前正式压力计划。\n"
            "<!-- CURRENT_FORMAL_STRESS_PLAN:END -->\n"
        )
        self.assertEqual(lines, ["当前正式压力计划。"])

    def test_markdown_prose_is_chinese(self) -> None:
        for relative in CHINESE_MARKDOWN:
            content = (ROOT / relative).read_text(encoding="utf-8")
            self.assertRegex(content, CJK_RE)
            for line in _markdown_prose_lines(content):
                # 纯数字表格行、分隔线和只含代码标识的行不属于自然语言。
                has_ascii_word = re.search(r"[A-Za-z]{4,}", line) is not None
                is_table_data = line.startswith("|") and CJK_RE.search(line) is None
                if has_ascii_word and not is_table_data:
                    self.assertRegex(
                        line,
                        CJK_RE,
                        msg=f"{relative} contains non-Chinese prose: {line}",
                    )

    def test_obsolete_documents_and_references_are_absent(self) -> None:
        tracked_text = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in _tracked_paths()
            if path != THIS_FILE
            and path.suffix in {".py", ".md", ".yml", ".yaml"}
        )
        for name in OBSOLETE_DOCUMENTS:
            self.assertNotIn(name, tracked_text)
            self.assertFalse((ROOT / name).exists())

    def test_readme_documents_every_default_strategy_parameter(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = [
            key
            for key in default_engine_config()
            if f"`{key}`" not in readme
        ]
        self.assertEqual(missing, [])

    def test_readme_documents_every_portfolio_policy_parameter(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = [
            key
            for key in PortfolioPolicy.__dataclass_fields__
            if f"`{key}`" not in readme
        ]
        self.assertEqual(missing, [])

    def test_readme_has_required_current_sections(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        required = (
            "## 当前策略结构",
            "## 默认策略参数",
            "## 组合策略参数",
            "## 优势",
            "## 缺点和已知限制",
            "## 适用行情",
            "## 不适用行情",
            "## 健壮性与灵活性",
            "## 还能继续提升的方向",
        )
        for heading in required:
            self.assertIn(heading, readme)


class SourceDocumentationTests(unittest.TestCase):
    """保证本轮维护模块的公共入口具有必要说明。"""

    def test_public_support_apis_have_docstrings(self) -> None:
        for filename in (
            "quantfusion/application/account_scan.py",
            "quantfusion/data/contracts.py",
            "scripts/benchmark_validation.py",
        ):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name.startswith("_"):
                        continue
                    self.assertIsNotNone(
                        ast.get_docstring(node),
                        msg=f"{filename}:{node.lineno} {node.name} lacks a docstring",
                    )


class RepositoryHygieneTests(unittest.TestCase):
    """保证本地缓存、编辑器配置和生成工件不会进入 Git 索引。"""

    def test_root_contains_only_public_entrypoints_and_project_configuration(self) -> None:
        tracked_root_files = {
            path.name for path in _tracked_paths() if len(path.parts) == 1
        }
        self.assertEqual(tracked_root_files, EXPECTED_ROOT_FILES)

    def test_repository_assets_are_grouped_by_responsibility(self) -> None:
        expected = (
            "artifacts/validation/candidates/stress-f5625e5b5813a5b58c52d076ad3c38e33d8b3292-rejected.json",
            "data/market/300308.csv",
            "data/regime/000300.csv",
            "examples/account.json",
            "scripts/backtest_universes.py",
            "tests/fixtures/backtest_golden_metrics.json",
            "tests/regression/test_quant_fusion.py",
        )
        for relative in expected:
            self.assertTrue((ROOT / relative).is_file(), msg=relative)

    def test_formal_stress_artifacts_are_absent_without_accepted_baseline(self) -> None:
        for name in ("prefix_stress.json", "universe_stress.json"):
            self.assertFalse(
                (ROOT / "artifacts" / "validation" / name).exists(),
                msg=name,
            )

    def test_validation_script_reads_the_single_golden_metrics_source(self) -> None:
        validation_script = importlib.import_module("scripts.run_regime_validation")
        self.assertFalse(hasattr(validation_script, "GOLDEN_BULL"))
        for name, codes in UNIVERSES.items():
            total_return, max_drawdown, total_trades = _golden_bull(name)
            self.assertIsInstance(total_return, float)
            self.assertLess(max_drawdown, 0.0)
            self.assertGreater(total_trades, 0)

    def test_persisted_validation_metadata_uses_portable_data_paths(self) -> None:
        for name in (
            "cambricon_universe_backtest.json",
            "universe_backtest.json",
        ):
            payload = json.loads(
                (ROOT / "artifacts" / "validation" / name).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["data_directory"], "data/market")
            if "regime_data_directory" in payload:
                self.assertEqual(payload["regime_data_directory"], "data/regime")

    def test_supported_command_modules_have_help(self) -> None:
        modules = (
            "quantfusion.application.backtest_cli",
            "quantfusion.application.daily_scan",
            "quantfusion.application.optimizer",
            "quantfusion.application.stress",
            "scripts.backtest_cambricon_universe",
            "scripts.backtest_universes",
            "scripts.benchmark_validation",
            "scripts.download_eastmoney_qfq",
            "scripts.run_regime_validation",
            "scripts.validate_basket",
        )
        for module in modules:
            completed = subprocess.run(
                [sys.executable, "-m", module, "--help"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"{module}: {completed.stderr}",
            )
            self.assertIn("usage:", completed.stdout.lower(), msg=module)

    def test_scripts_use_module_execution_without_path_bootstrap(self) -> None:
        self.assertFalse((ROOT / "scripts/_bootstrap.py").exists())
        for path in (ROOT / "scripts").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("_bootstrap", source, msg=str(path))
            self.assertNotIn("if __package__", source, msg=str(path))
            self.assertNotIn("sys.path", source, msg=str(path))

    def test_only_canonical_repository_data_defaults_are_exported(self) -> None:
        public_config = importlib.import_module("quantfusion.config")
        self.assertEqual(
            repository_paths.MARKET_DATA_DIR,
            repository_paths.DATA_ROOT / "market",
        )
        self.assertEqual(
            repository_paths.REGIME_DATA_DIR,
            repository_paths.DATA_ROOT / "regime",
        )
        self.assertFalse(hasattr(repository_paths, "resolve_repository_data_dir"))
        self.assertFalse(hasattr(public_config, "resolve_repository_data_dir"))

    def test_forbidden_portfolio_alias_module_is_absent(self) -> None:
        self.assertFalse((ROOT / "quantfusion/portfolio/policy.py").exists())

    def test_ci_uses_the_supported_python_311_lockfile(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        py311_lock = ROOT / "requirements-lock-py311.txt"

        self.assertTrue(py311_lock.exists())
        self.assertIn("--python-version 3.11", py311_lock.read_text(encoding="utf-8"))
        self.assertRegex(
            workflow,
            r'python-version: "3\.11"\s+lock-file: requirements-lock-py311\.txt',
        )
        self.assertRegex(
            workflow,
            r'python-version: "3\.11"\s+'
            r'lock-file: requirements-lock-py311\.txt\s+'
            r'pytest-args: .+economic_sequences_match_frozen_fingerprints',
        )
        self.assertIn("cache-dependency-path: ${{ matrix.lock-file }}", workflow)
        self.assertIn("pip install -r ${{ matrix.lock-file }}", workflow)
        self.assertIn("pytest -q ${{ matrix.pytest-args }}", workflow)

    def test_c6_bound_workflow_keeps_history_and_fencing_token_out_of_inputs(self) -> None:
        workflow = (
            ROOT / ".github/workflows/c6-bound-economic.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("      fencing_token:\n", workflow)
        self.assertIn("      - name: Initialize run-local fencing token\n", workflow)
        self.assertGreaterEqual(workflow.count("          fetch-depth: 0\n"), 2)
        self.assertNotIn(
            "C6_FENCING_TOKEN: ${{ inputs.fencing_token }}",
            workflow,
        )

    def test_generated_directories_are_not_committed(self) -> None:
        forbidden_parts = {
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".pyright",
            ".venv",
            "venv",
            ".idea",
            ".vscode",
            "daily_signals",
            "data_cache",
            "optimizer_output",
        }
        violations = [
            str(path)
            for path in _tracked_paths()
            if forbidden_parts.intersection(path.parts)
        ]
        self.assertEqual(violations, [])

    def test_generated_files_are_not_committed(self) -> None:
        forbidden_names = {
            ".DS_Store",
            ".coverage",
            "benchmark_validation.json",
            "live_refresh_manifest.json",
        }
        violations = [
            str(path)
            for path in _tracked_paths()
            if path.name in forbidden_names
            or path.suffix in {".pyc", ".pyo", ".log", ".tmp"}
        ]
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
