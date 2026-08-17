"""Canonical package boundaries and legacy compatibility contracts."""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "quantfusion"
LEGACY_MODULES = {
    "quant_fusion",
    "regime_adaptive",
    "cross_market_overlay",
    "risk_governance",
    "account_signal_engine",
    "market_data_contracts",
    "daily_signal_scan",
    "quant_fusion_optimizer",
    "stress_test_prefixes",
}
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
ROOT_SIZE_LIMITS = {
    "quant_fusion.py": 300,
    "daily_signal_scan.py": 180,
    "regime_adaptive.py": 250,
    "cross_market_overlay.py": 150,
    "account_signal_engine.py": 150,
    "stress_test_prefixes.py": 150,
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


class CanonicalLayoutTests(unittest.TestCase):
    """The repository exposes one understandable canonical package."""

    def test_required_packages_exist(self) -> None:
        self.assertTrue(PACKAGE.is_dir(), "quantfusion package is missing")
        missing = sorted(
            name for name in REQUIRED_PACKAGES
            if not (PACKAGE / name / "__init__.py").is_file()
        )
        self.assertEqual(missing, [])

    def test_legacy_roots_are_thin_facades_or_clis(self) -> None:
        oversized = {
            name: len((ROOT / name).read_text(encoding="utf-8").splitlines())
            for name, limit in ROOT_SIZE_LIMITS.items()
            if len((ROOT / name).read_text(encoding="utf-8").splitlines()) > limit
        }
        self.assertEqual(oversized, {})


class DependencyDirectionTests(unittest.TestCase):
    """Canonical modules depend inward and never back through legacy roots."""

    def test_canonical_package_never_imports_legacy_modules(self) -> None:
        violations: list[str] = []
        for path in PACKAGE.rglob("*.py") if PACKAGE.exists() else ():
            for module, _ in _imports(path):
                if module.split(".", 1)[0] in LEGACY_MODULES:
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

    def test_cross_package_objects_do_not_access_private_attributes(self) -> None:
        violations: list[str] = []
        for path in PACKAGE.rglob("*.py") if PACKAGE.exists() else ():
            relative = path.relative_to(PACKAGE)
            if len(relative.parts) < 2:
                continue
            owner = relative.parts[0]
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            external_bindings: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if len(parts) >= 2 and parts[0] == "quantfusion" and parts[1] != owner:
                            external_bindings.add(alias.asname or parts[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if len(parts) >= 2 and parts[0] == "quantfusion" and parts[1] != owner:
                        external_bindings.update(alias.asname or alias.name for alias in node.names)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr.startswith("_")
                    and isinstance(node.value, ast.Name)
                    and node.value.id in external_bindings
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} -> "
                        f"{node.value.id}.{node.attr}"
                    )
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


class CompatibilityTests(unittest.TestCase):
    """Legacy imports resolve to the canonical implementation."""

    def test_quant_fusion_public_api_remains_available(self) -> None:
        legacy = importlib.import_module("quant_fusion")
        required = {
            "BacktestEngine",
            "SleeveBacktestEngine",
            "PortfolioPolicy",
            "DataFetcher",
            "Indicators",
            "Position",
            "Signal",
            "TradeRecord",
            "PerformanceReport",
            "build_argument_parser",
            "parse_symbols",
        }
        self.assertEqual(sorted(required - set(dir(legacy))), [])

    def test_default_config_has_one_public_source(self) -> None:
        legacy = importlib.import_module("quant_fusion")
        config = importlib.import_module("quantfusion.config.engine")
        self.assertEqual(
            legacy._CoreBacktestEngine._default_config(),
            config.default_engine_config(),
        )

    def test_stress_cli_delegates_to_canonical_application(self) -> None:
        legacy = importlib.import_module("stress_test_prefixes")
        canonical = importlib.import_module("quantfusion.application.stress")
        self.assertIs(legacy, canonical)


if __name__ == "__main__":
    unittest.main()
