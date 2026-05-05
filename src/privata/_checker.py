"""Detect module privacy issues within Python source roots."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from privata._entrypoints import collect_external_entrypoints, load_tach_interface_exports
from privata._imports import collect_private_module_imports, find_cross_imports
from privata._modules import collect_modules
from privata._source_roots import source_roots

if TYPE_CHECKING:
    from privata._models import PrivateModuleImport, Symbol


def _collect_privacy_findings(project_root: Path) -> tuple[list[Symbol], list[PrivateModuleImport]]:
    """Collect public-symbol and private-module boundary findings."""
    modules = collect_modules(source_roots(project_root))
    cross_imports = find_cross_imports(modules)
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
    private_module_imports = collect_private_module_imports(modules)
    return candidates, private_module_imports


def find_private_candidates(project_root: Path) -> list[Symbol]:
    """Find symbols that appear module-local and should be private."""
    candidates, _ = _collect_privacy_findings(project_root)
    return candidates


def find_private_module_imports(project_root: Path) -> list[PrivateModuleImport]:
    """Find private modules imported from outside their package subtree."""
    _, private_module_imports = _collect_privacy_findings(project_root)
    return private_module_imports


def main() -> int:
    """Entry point: scan project and report module-local public symbols."""
    project_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    candidates, private_module_imports = _collect_privacy_findings(project_root)

    if not candidates and not private_module_imports:
        print("No module privacy issues found.")
        return 0

    if candidates:
        print(f"Found {len(candidates)} public symbols that could be made private:\n")
        for sym in candidates:
            rel = sym.path.relative_to(project_root)
            print(f"  {rel}:{sym.lineno}: {sym.kind} `{sym.name}`")

    if private_module_imports:
        if candidates:
            print()
        print(
            "Found "
            f"{len(private_module_imports)} "
            "private module imports outside their package subtree:\n",
        )
        for issue in private_module_imports:
            rel = issue.imported_by_path.relative_to(project_root)
            print(f"  {rel}:{issue.lineno}: imports private module `{issue.module}`")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
