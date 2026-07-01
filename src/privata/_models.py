"""Shared data models for privacy checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ast
    from pathlib import Path


@dataclass
class Symbol:
    """A public top-level symbol found in a module."""

    name: str
    kind: str
    lineno: int
    module: str
    path: Path


@dataclass
class Module:
    """A parsed Python module with its top-level symbols."""

    name: str
    path: Path
    package_parts: tuple[str, ...]
    symbols: list[Symbol] = field(default_factory=list)
    private_symbols: list[Symbol] = field(default_factory=list)
    tree: ast.Module | None = None
    ignored_lines: frozenset[int] = frozenset()


@dataclass
class PrivateModuleImport:
    """A private module imported from outside its containing package subtree."""

    module: str
    path: Path
    imported_by: str
    imported_by_path: Path
    lineno: int


@dataclass
class PrivateSymbolImport:
    """A private top-level symbol imported from another production module."""

    module: str
    name: str
    path: Path
    imported_by: str
    imported_by_path: Path
    lineno: int


@dataclass
class ExportIssue:
    """A mismatch between literal __all__ and public module bindings."""

    module: str
    path: Path
    name: str
    kind: str
    lineno: int


@dataclass(frozen=True)
class SymbolCandidate:
    """A candidate top-level symbol before filtering."""

    name: str
    kind: str
    lineno: int
