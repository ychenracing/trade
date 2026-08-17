"""仓库结构、中文文档和文档—代码一致性的回归测试。"""

from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

from quantfusion.config.engine import default_engine_config
from quantfusion.config.portfolio import PortfolioPolicy


ROOT = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve().relative_to(ROOT)
EXPECTED_MARKDOWN = {
    Path("README.md"),
    Path("BACKTEST_RESULTS.md"),
    Path("TRANSFORMATION_REPORT.md"),
    Path("docs/ARCHITECTURE.md"),
    Path("docs/superpowers/plans/2026-08-17-engineering-refactor.md"),
    Path("historical_data/README.md"),
}
OBSOLETE_DOCUMENTS = {
    "STRATEGY_REVIEW.md",
    "PRODUCTION_REVIEW_FIXES.md",
    "REGIME_ADAPTIVE_REFACTOR_REPORT.md",
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
        if not in_fence and stripped:
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
    return tuple(
        Path(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
    )


class MarkdownConsistencyTests(unittest.TestCase):
    """保证仓库只保留当前有效中文文档。"""

    def test_only_current_markdown_documents_are_kept(self) -> None:
        found = {
            path.relative_to(ROOT)
            for path in ROOT.rglob("*.md")
            if ".git" not in path.parts
            and "optimizer_validation" not in path.parts
            and ".pytest_cache" not in path.parts
        }
        self.assertEqual(found, EXPECTED_MARKDOWN)

    def test_markdown_prose_is_chinese(self) -> None:
        for relative in EXPECTED_MARKDOWN:
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
            "account_signal_engine.py",
            "market_data_contracts.py",
            "benchmark_validation.py",
        ):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name.startswith("_"):
                        continue
                    self.assertIsNotNone(
                        ast.get_docstring(node),
                        msg=f"{filename}:{node.lineno} {node.name} lacks a docstring",
                    )


class RepositoryHygieneTests(unittest.TestCase):
    """保证本地缓存、编辑器配置和生成工件不会进入 Git 索引。"""

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
