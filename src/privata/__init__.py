"""Python module privacy checks."""

from privata._checker import (
    find_export_issues,
    find_private_candidates,
    find_private_module_imports,
    find_private_symbol_imports,
)
from privata._imports import (
    collect_private_module_imports,
    collect_private_symbol_imports,
    find_cross_imports,
)
from privata._models import ExportIssue, Module, PrivateModuleImport, PrivateSymbolImport, Symbol
from privata._modules import collect_modules

try:
    from privata._version import __version__
except ImportError:  # pragma: no cover - only used from editable trees before hatch-vcs writes it
    __version__ = "0.0.0"

__all__ = [
    "ExportIssue",
    "Module",
    "PrivateModuleImport",
    "PrivateSymbolImport",
    "Symbol",
    "__version__",
    "collect_modules",
    "collect_private_module_imports",
    "collect_private_symbol_imports",
    "find_cross_imports",
    "find_export_issues",
    "find_private_candidates",
    "find_private_module_imports",
    "find_private_symbol_imports",
]
