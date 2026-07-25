"""Detection of public methods that no other production module references."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._models import Method

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from privata._models import Module

# Decorators that leave a method free to be renamed. Anything else may register
# the method under its current name, so decorated methods are left alone.
_SAFE_METHOD_DECORATORS = frozenset(
    {
        "cache",
        "cached_property",
        "classmethod",
        "final",
        "lru_cache",
        "property",
        "staticmethod",
    },
)
_SAFE_CLASS_DECORATORS = frozenset({"dataclass", "final"})


def collect_method_candidates(
    modules: Mapping[str, Module],
    public_interface: set[tuple[str, str]] | None = None,
    test_references: Mapping[str, set[str]] | None = None,
) -> list[Method]:
    """Return public methods that only their own module refers to.

    A method counts as used when another production module mentions its name,
    either as an attribute access or as a string literal (for ``getattr`` style
    lookups). ``test_references`` supplies extra names for helper modules that
    live in a test source root, mirroring the test-helper rule for symbols.
    """
    interface = public_interface if public_interface is not None else set()
    extra_references = test_references if test_references is not None else {}
    references = _references_by_module(modules)

    candidates: list[Method] = []
    for module in modules.values():
        if module.tree is None:
            continue
        for node in module.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_checkable_class(node, module, interface):
                continue
            candidates.extend(
                _class_method_candidates(
                    module,
                    node,
                    references,
                    extra_references.get(module.name, set()),
                ),
            )

    candidates.sort(key=lambda method: (str(method.path), method.lineno))
    return candidates


def referenced_names(module: Module) -> set[str]:
    """Return every attribute name and string literal a module mentions."""
    if module.tree is None:
        return set()

    names: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _references_by_module(modules: Mapping[str, Module]) -> dict[str, set[str]]:
    """Map each referenced name to the modules that mention it."""
    references: dict[str, set[str]] = {}
    for module_name, module in modules.items():
        for name in referenced_names(module):
            references.setdefault(name, set()).add(module_name)
    return references


def _class_method_candidates(
    module: Module,
    class_node: ast.ClassDef,
    references: Mapping[str, set[str]],
    test_references: set[str],
) -> Iterator[Method]:
    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_checkable_method(node):
            continue
        if node.lineno in module.ignored_lines:
            continue
        if node.name in test_references:
            continue
        if references.get(node.name, set()) - {module.name}:
            continue
        yield Method(
            name=node.name,
            class_name=class_node.name,
            lineno=node.lineno,
            module=module.name,
            path=module.path,
        )


def _is_checkable_class(
    node: ast.ClassDef,
    module: Module,
    public_interface: set[tuple[str, str]],
) -> bool:
    """Return whether a class owns its method names outright.

    Only plain, public, non-exported classes qualify. A base class Privata
    cannot see may require a method to keep its public name, and an exported
    class exposes its methods as part of the package interface.
    """
    if node.name.startswith("_"):
        return False
    if node.name in module.exports or (module.name, node.name) in public_interface:
        return False
    if node.keywords:
        return False
    if any(_dotted_name(base) != "object" for base in node.bases):
        return False
    return _has_only_safe_decorators(node.decorator_list, _SAFE_CLASS_DECORATORS)


def _is_checkable_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if node.name.startswith("_"):
        return False
    return _has_only_safe_decorators(node.decorator_list, _SAFE_METHOD_DECORATORS)


def _has_only_safe_decorators(decorators: list[ast.expr], safe_names: frozenset[str]) -> bool:
    return all(_decorator_name(decorator) in safe_names for decorator in decorators)


def _decorator_name(decorator: ast.expr) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    dotted = _dotted_name(target)
    if dotted is None:
        return None
    return dotted.rsplit(".", 1)[-1]


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None
