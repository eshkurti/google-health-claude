"""Import rules between the package's layers, read from the source with ast.

Every import counts, including ones inside functions (cli imports its commands
lazily). Relative imports are resolved against the importing module, so
``from ..fetch import run`` in derive/x.py is seen as google_health_claude.fetch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = "google_health_claude"
SRC = Path(__file__).resolve().parents[1] / "src" / PACKAGE


def module_name(path: Path) -> str:
    parts = [PACKAGE, *path.relative_to(SRC).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def modules() -> dict[str, Path]:
    return {module_name(p): p for p in sorted(SRC.rglob("*.py"))}


def imports_of(path: Path) -> set[str]:
    """Package modules this file imports (``from x import y`` adds x and x.y)."""
    name = module_name(path)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                target = ".".join([*base, node.module] if node.module else base)
            else:
                target = node.module or ""
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
    return {i for i in found if i == PACKAGE or i.startswith(PACKAGE + ".")}


def within(name: str, layer: str) -> bool:
    full = f"{PACKAGE}.{layer}"
    return name == full or name.startswith(full + ".")


def violations(importers: list[str], forbidden: list[str]) -> list[str]:
    all_modules = modules()
    return sorted(
        f"{module} imports {imported}"
        for module in importers
        for imported in imports_of(all_modules[module])
        if any(within(imported, layer) for layer in forbidden)
    )


def test_the_import_reader_resolves_relative_imports() -> None:
    """Guards the rules below against passing because nothing was found."""
    all_modules = modules()
    assert {"google_health_claude.cli", "google_health_claude.derive.load"} <= set(all_modules)
    assert f"{PACKAGE}.records" in imports_of(all_modules[f"{PACKAGE}.derive.values"])
    assert f"{PACKAGE}.derive.load" in imports_of(all_modules[f"{PACKAGE}.derive"])
    cli = imports_of(all_modules[f"{PACKAGE}.cli"])
    assert f"{PACKAGE}.devtools.probe" in cli and f"{PACKAGE}.demo" in cli
    assert f"{PACKAGE}.fetch.tcx" in imports_of(all_modules[f"{PACKAGE}.demo.generate"])
    assert violations([f"{PACKAGE}.cli"], ["devtools"])  # a rule fires when broken


def test_derive_never_imports_api_or_fetch() -> None:
    derive = [m for m in modules() if within(m, "derive")]
    assert derive
    assert violations(derive, ["api", "fetch"]) == []


@pytest.mark.parametrize("layer", ["devtools", "demo"])
def test_only_cli_imports_devtools_and_demo(layer: str) -> None:
    others = [m for m in modules() if m != f"{PACKAGE}.cli" and not within(m, layer)]
    assert violations(others, [layer]) == []


@pytest.mark.parametrize("module", ["records", "config"])
def test_records_and_config_import_nothing_from_the_package(module: str) -> None:
    assert imports_of(modules()[f"{PACKAGE}.{module}"]) == set()
