"""Canonical package boundaries and forbidden root-surface contracts."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "quantfusion"
FORBIDDEN_ROOT_FILES = {
    "account_signal_engine.py",
    "backtest_cambricon_universe.py",
    "backtest_universes.py",
    "benchmark_validation.py",
    "cross_market_overlay.py",
    "daily_signal_scan.py",
    "download_eastmoney_qfq.py",
    "market_data_contracts.py",
    "quant_fusion.py",
    "quant_fusion_optimizer.py",
    "regime_adaptive.py",
    "risk_governance.py",
    "run_regime_validation.py",
    "stress_test_prefixes.py",
    "validate_basket.py",
}
DELETED_ROOT_MODULES = {path.removesuffix(".py") for path in FORBIDDEN_ROOT_FILES}
REQUIRED_PACKAGES = {
    "domain",
    "config",
    "data",
    "indicators",
    "strategy",
    "execution",
    "portfolio",
    "risk",
    "regime",
    "engine",
    "account",
    "application",
    "io",
    "research",
}
ALLOWED_DEPENDENCIES = {
    "domain": set(),
    "config": {"domain"},
    "data": {"domain", "config"},
    "indicators": {"domain", "config"},
    "strategy": {"domain", "config", "indicators"},
    "execution": {"domain", "config"},
    "portfolio": {"domain", "config", "execution"},
    "risk": {
        "domain", "config", "data", "indicators", "execution", "portfolio"
    },
    "regime": {"domain", "config", "data", "indicators"},
    "engine": {
        "domain", "config", "data", "indicators", "strategy", "execution",
        "portfolio", "risk", "regime",
    },
    "account": {
        "domain", "config", "data", "indicators", "strategy", "risk", "regime"
    },
    "io": {"domain", "config"},
    "application": {
        "domain", "config", "data", "indicators", "strategy", "execution",
        "portfolio", "risk", "regime", "engine", "account", "io", "research",
    },
    "research": {"domain", "config", "data", "engine", "regime"},
}
def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    return ".".join(relative.parts)


def _imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, tuple(alias.name for alias in node.names)))
    return found


def _attribute_parts(node: ast.Attribute) -> tuple[str, ...]:
    """Return the dotted-name parts for one attribute expression."""
    parts = [node.attr]
    value: ast.expr = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return tuple(reversed(parts))


class CanonicalLayoutTests(unittest.TestCase):
    """The repository exposes one understandable canonical package."""

    def test_required_packages_exist(self) -> None:
        self.assertTrue(PACKAGE.is_dir(), "quantfusion package is missing")
        missing = sorted(
            name for name in REQUIRED_PACKAGES
            if not (PACKAGE / name / "__init__.py").is_file()
        )
        self.assertEqual(missing, [])

    def test_forbidden_root_modules_are_absent(self) -> None:
        present = sorted(name for name in FORBIDDEN_ROOT_FILES if (ROOT / name).exists())
        self.assertEqual(present, [])


class DependencyDirectionTests(unittest.TestCase):
    """Canonical modules depend inward and never through deleted root modules."""

    def test_canonical_package_never_imports_deleted_root_modules(self) -> None:
        violations: list[str] = []
        for path in PACKAGE.rglob("*.py") if PACKAGE.exists() else ():
            for module, _ in _imports(path):
                if module.split(".", 1)[0] in DELETED_ROOT_MODULES:
                    violations.append(f"{path.relative_to(ROOT)} -> {module}")
        self.assertEqual(violations, [])

    def test_dependency_layers_do_not_point_outward(self) -> None:
        violations: list[str] = []
        for path in PACKAGE.rglob("*.py") if PACKAGE.exists() else ():
            relative = path.relative_to(PACKAGE)
            if len(relative.parts) < 2:
                continue
            owner = relative.parts[0]
            for module, _ in _imports(path):
                parts = module.split(".")
                if len(parts) < 2 or parts[0] != "quantfusion":
                    continue
                dependency = parts[1]
                if dependency != owner and dependency not in ALLOWED_DEPENDENCIES[owner]:
                    violations.append(
                        f"{path.relative_to(ROOT)} ({owner}) -> {module} ({dependency})"
                    )
        self.assertEqual(violations, [])

    def test_cross_package_imports_do_not_use_private_names(self) -> None:
        violations: list[str] = []
        for path in PACKAGE.rglob("*.py") if PACKAGE.exists() else ():
            relative = path.relative_to(PACKAGE)
            if len(relative.parts) < 2:
                continue
            owner = relative.parts[0]
            for module, names in _imports(path):
                parts = module.split(".")
                if len(parts) < 2 or parts[0] != "quantfusion":
                    continue
                if parts[1] == owner:
                    continue
                for name in names:
                    if name.startswith("_"):
                        violations.append(
                            f"{path.relative_to(ROOT)} -> {module}.{name}"
                        )
        self.assertEqual(violations, [])

    def test_cross_package_objects_do_not_use_private_attributes(self) -> None:
        """Reject ``ImportedType._private`` dependencies across packages."""
        violations: list[str] = []
        for path in PACKAGE.rglob("*.py") if PACKAGE.exists() else ():
            relative = path.relative_to(PACKAGE)
            if len(relative.parts) < 2:
                continue
            owner = relative.parts[0]
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            bindings: dict[str, str] = {}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module:
                    module_parts = node.module.split(".")
                    if len(module_parts) >= 2 and module_parts[0] == "quantfusion":
                        for alias in node.names:
                            bindings[alias.asname or alias.name] = module_parts[1]
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module_parts = alias.name.split(".")
                        if len(module_parts) >= 2 and module_parts[0] == "quantfusion":
                            bindings[alias.asname or module_parts[0]] = module_parts[1]
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
                    continue
                parts = _attribute_parts(node)
                if not parts:
                    continue
                dependency = bindings.get(parts[0])
                if parts[0] == "quantfusion" and len(parts) >= 2:
                    dependency = parts[1]
                if dependency and dependency != owner:
                    violations.append(
                        f"{path.relative_to(ROOT)} -> {'.'.join(parts)}"
                    )
        self.assertEqual(sorted(set(violations)), [])

    def test_overlay_policy_does_not_mutate_pending_queues(self) -> None:
        """Only the overlay adapter may know the engine pending container."""
        violations: list[str] = []
        policy_files = (
            PACKAGE / "risk" / "overlay" / "actions.py",
            PACKAGE / "risk" / "overlay" / "policy_base.py",
        )
        for path in policy_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "pending":
                    violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(violations, [])

    def test_canonical_import_graph_is_acyclic(self) -> None:
        paths = list(PACKAGE.rglob("*.py")) if PACKAGE.exists() else []
        known = {_module_name(path): path for path in paths}
        graph: dict[str, set[str]] = {name: set() for name in known}
        for name, path in known.items():
            for module, _ in _imports(path):
                if module in known:
                    graph[name].add(module)
                elif module + ".__init__" in known:
                    graph[name].add(module + ".__init__")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str, trail: tuple[str, ...]) -> None:
            if name in visiting:
                cycle = " -> ".join((*trail, name))
                self.fail(f"canonical import cycle: {cycle}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in sorted(graph[name]):
                visit(dependency, (*trail, name))
            visiting.remove(name)
            visited.add(name)

        for name in sorted(graph):
            visit(name, ())


class CanonicalPublicApiTests(unittest.TestCase):
    """Current public imports resolve from the canonical package."""

    def test_canonical_package_public_api_is_available(self) -> None:
        """The public imports used by applications must resolve."""
        from quantfusion.config import (
            PortfolioPolicy,
            default_engine_config,
            validate_engine_config,
        )
        from quantfusion.data import DataFetcher
        from quantfusion.domain import Position, Signal, TradeRecord
        from quantfusion.engine import BacktestEngine, SleeveBacktestEngine

        self.assertTrue(callable(BacktestEngine))
        self.assertTrue(callable(SleeveBacktestEngine))
        self.assertTrue(callable(PortfolioPolicy))
        self.assertTrue(callable(DataFetcher))
        self.assertTrue(callable(Position))
        self.assertTrue(callable(Signal))
        self.assertTrue(callable(TradeRecord))
        defaults = default_engine_config()
        self.assertEqual(validate_engine_config(defaults), defaults)

if __name__ == "__main__":
    unittest.main()
