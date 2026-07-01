"""Detect module privacy issues within Python source roots."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from privata._entrypoints import collect_external_entrypoints, load_tach_interface_exports
from privata._exports import collect_export_issues
from privata._imports import (
    collect_private_module_imports,
    collect_private_symbol_imports,
    find_cross_imports,
)
from privata._modules import collect_module_collisions, collect_modules, collect_test_consumers
from privata._source_roots import is_test_source_root, source_roots

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from privata._models import (
        ExportIssue,
        Module,
        ModuleCollision,
        PrivateModuleImport,
        PrivateSymbolImport,
        Symbol,
    )


class _PrivacyFindings(NamedTuple):
    """All findings produced by one scan of a project."""

    candidates: list[Symbol]
    private_module_imports: list[PrivateModuleImport]
    private_symbol_imports: list[PrivateSymbolImport]
    export_issues: list[ExportIssue]
    module_collisions: list[ModuleCollision]


def _test_helper_cross_imports(
    roots: list[Path],
    modules: dict[str, Module],
) -> set[tuple[str, str]]:
    """Return helper-module symbols in test source roots that co-located test files use.

    Each pass is scoped to a single test root so that test files can only certify
    helper modules in their own root, never production symbols.
    """
    test_roots = [root for root in roots if is_test_source_root(root)]
    test_consumers = collect_test_consumers(test_roots)
    used: set[tuple[str, str]] = set()
    for root in test_roots:
        helpers = {
            name: module for name, module in modules.items() if module.path.is_relative_to(root)
        }
        consumers = {
            name: module
            for name, module in test_consumers.items()
            if module.path.is_relative_to(root)
        }
        used |= find_cross_imports(helpers, consumers)
    return used


def _collect_privacy_findings(project_root: Path) -> _PrivacyFindings:
    """Collect public-symbol and private-module boundary findings."""
    roots = source_roots(project_root)
    modules = collect_modules(roots)
    cross_imports = find_cross_imports(modules) | _test_helper_cross_imports(roots, modules)
    external_entrypoints = collect_external_entrypoints(project_root)
    public_interface_exports = load_tach_interface_exports(project_root)

    candidates = [
        sym
        for mod in modules.values()
        for sym in mod.symbols
        if (sym.module, sym.name) not in cross_imports
        and (sym.module, sym.name) not in external_entrypoints
        and (sym.module, sym.name) not in public_interface_exports
    ]
    candidates.sort(key=lambda s: (str(s.path), s.lineno))
    return _PrivacyFindings(
        candidates=candidates,
        private_module_imports=collect_private_module_imports(modules),
        private_symbol_imports=collect_private_symbol_imports(modules),
        export_issues=collect_export_issues(modules),
        module_collisions=collect_module_collisions(roots),
    )


def find_private_candidates(project_root: Path) -> list[Symbol]:
    """Find symbols that appear module-local and should be private."""
    return _collect_privacy_findings(project_root).candidates


def find_private_module_imports(project_root: Path) -> list[PrivateModuleImport]:
    """Find private modules imported from outside their package subtree."""
    return _collect_privacy_findings(project_root).private_module_imports


def find_private_symbol_imports(project_root: Path) -> list[PrivateSymbolImport]:
    """Find private top-level symbols imported from another production module."""
    return _collect_privacy_findings(project_root).private_symbol_imports


def find_export_issues(project_root: Path) -> list[ExportIssue]:
    """Find literal __all__ declarations that are stale or incomplete."""
    return _collect_privacy_findings(project_root).export_issues


def find_module_collisions(project_root: Path) -> list[ModuleCollision]:
    """Find module names that resolve to more than one file across source roots."""
    return _collect_privacy_findings(project_root).module_collisions


def check_project(project_root: Path) -> int:
    """Scan project and report module-local public symbols."""
    project_root = project_root.resolve()
    findings = _collect_privacy_findings(project_root)

    sections: list[tuple[bool, Callable[[], None]]] = [
        (
            bool(findings.module_collisions),
            lambda: _print_module_collisions(findings.module_collisions, project_root),
        ),
        (
            bool(findings.candidates),
            lambda: _print_private_candidates(findings.candidates, project_root),
        ),
        (
            bool(findings.private_module_imports),
            lambda: _print_private_module_imports(findings.private_module_imports, project_root),
        ),
        (
            bool(findings.private_symbol_imports),
            lambda: _print_private_symbol_imports(findings.private_symbol_imports, project_root),
        ),
        (
            bool(findings.export_issues),
            lambda: _print_export_issues(findings.export_issues, project_root),
        ),
    ]
    printers = [printer for has_findings, printer in sections if has_findings]
    if not printers:
        print("No module privacy issues found.")
        return 0

    for index, printer in enumerate(printers):
        if index:
            print()
        printer()

    return 1


def _print_module_collisions(collisions: list[ModuleCollision], project_root: Path) -> None:
    print(
        f"Found {len(collisions)} module names defined by multiple files; "
        "only one file per name is scanned, so findings for these modules "
        "may be incomplete:\n",
    )
    for collision in collisions:
        rels = ", ".join(path.relative_to(project_root).as_posix() for path in collision.paths)
        print(f"  module `{collision.module}` is defined by: {rels}")


def _print_private_candidates(candidates: list[Symbol], project_root: Path) -> None:
    print(f"Found {len(candidates)} public symbols that could be made private:\n")
    for symbol in candidates:
        rel = symbol.path.relative_to(project_root).as_posix()
        print(f"  {rel}:{symbol.lineno}: {symbol.kind} `{symbol.name}`")


def _print_private_module_imports(
    private_module_imports: list[PrivateModuleImport],
    project_root: Path,
) -> None:
    print(
        "Found "
        f"{len(private_module_imports)} "
        "private module imports outside their package subtree:\n",
    )
    for private_import in private_module_imports:
        rel = private_import.imported_by_path.relative_to(project_root).as_posix()
        print(f"  {rel}:{private_import.lineno}: imports private module `{private_import.module}`")


def _print_private_symbol_imports(
    private_symbol_imports: list[PrivateSymbolImport],
    project_root: Path,
) -> None:
    print(
        f"Found {len(private_symbol_imports)} private symbol imports from production modules:\n",
    )
    for private_import in private_symbol_imports:
        rel = private_import.imported_by_path.relative_to(project_root).as_posix()
        print(
            f"  {rel}:{private_import.lineno}: imports private symbol "
            f"`{private_import.module}.{private_import.name}`",
        )


def _print_export_issues(export_issues: list[ExportIssue], project_root: Path) -> None:
    print(f"Found {len(export_issues)} __all__ export issues:\n")
    for export_issue in export_issues:
        rel = export_issue.path.relative_to(project_root).as_posix()
        if export_issue.kind == "unknown":
            print(
                f"  {rel}:{export_issue.lineno}: "
                f"__all__ exports unknown name `{export_issue.name}`",
            )
        elif export_issue.kind == "private":
            print(
                f"  {rel}:{export_issue.lineno}: "
                f"__all__ exports private name `{export_issue.name}`",
            )
        else:
            print(
                f"  {rel}:{export_issue.lineno}: "
                f"public name `{export_issue.name}` missing from __all__",
            )
