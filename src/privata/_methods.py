"""Detection of public methods that no other production module references."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._models import Method

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from privata._models import Module

# Decorators that leave a method free to be renamed. Canonical names avoid
# treating unrelated decorators with a trusted basename as rename-safe.
_SAFE_METHOD_DECORATORS = frozenset(
    {
        "builtins.classmethod",
        "builtins.property",
        "builtins.staticmethod",
        "functools.cache",
        "functools.cached_property",
        "functools.lru_cache",
        "typing.final",
        "typing_extensions.final",
    },
)
_SAFE_CLASS_DECORATORS = frozenset(
    {
        "dataclasses.dataclass",
        "typing.final",
        "typing_extensions.final",
    },
)
_BUILTIN_DECORATORS = {
    "classmethod": "builtins.classmethod",
    "property": "builtins.property",
    "staticmethod": "builtins.staticmethod",
}


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
        decorator_aliases = _decorator_aliases(module.tree)
        for node in module.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_checkable_class(node, module, interface, decorator_aliases):
                continue
            candidates.extend(
                _class_method_candidates(
                    module,
                    node,
                    references,
                    extra_references.get(module.name, set()),
                    decorator_aliases,
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
    decorator_aliases: Mapping[str, str],
) -> Iterator[Method]:
    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_checkable_method(node, decorator_aliases):
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
    decorator_aliases: Mapping[str, str],
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
    return _has_only_safe_decorators(
        node.decorator_list,
        _SAFE_CLASS_DECORATORS,
        decorator_aliases,
    )


def _is_checkable_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    decorator_aliases: Mapping[str, str],
) -> bool:
    if node.name.startswith("_"):
        return False
    return _has_only_safe_decorators(
        node.decorator_list,
        _SAFE_METHOD_DECORATORS,
        decorator_aliases,
    )


def _has_only_safe_decorators(
    decorators: list[ast.expr],
    safe_names: frozenset[str],
    aliases: Mapping[str, str],
) -> bool:
    return all(_decorator_name(decorator, aliases) in safe_names for decorator in decorators)


def _decorator_name(decorator: ast.expr, aliases: Mapping[str, str]) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    dotted = _dotted_name(target)
    if dotted is None:
        return None
    head, separator, tail = dotted.partition(".")
    resolved = aliases.get(head, head)
    return f"{resolved}.{tail}" if separator else resolved


def _decorator_aliases(tree: ast.Module) -> dict[str, str]:  # noqa: C901
    """Return canonical module-level names used by decorator expressions."""
    aliases = dict(_BUILTIN_DECORATORS)

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                imported = alias.name if alias.asname else local
                aliases[local] = imported
        elif isinstance(node, ast.ImportFrom):
            source = f"{'.' * node.level}{node.module or ''}"
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{source}.{alias.name}"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            aliases[node.name] = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for child in ast.walk(target):
                    if isinstance(child, ast.Name):
                        aliases[child.id] = child.id
    return aliases


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None
