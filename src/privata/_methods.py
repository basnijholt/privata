"""Detection of public methods that no other production module references."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._imports import resolve_import_source
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
_LOCAL_DECORATOR_PREFIX = "<local>"


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
            decorator_aliases = _decorator_aliases(module.tree, node.lineno)
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


def _referenced_names(module: Module) -> set[str]:
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


def referenced_names_by_module(  # noqa: C901
    module: Module,
    known_modules: Mapping[str, Module],
) -> dict[str, set[str]]:
    """Return referenced names attributed to imported modules."""
    if module.tree is None:
        return {}

    bindings = _imported_module_bindings(
        module.tree,
        module.package_parts,
        known_modules,
    )
    assignments = sorted(
        (node for node in ast.walk(module.tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for assignment in assignments:
        if assignment.value is not None:
            sources = _expression_modules(assignment.value, bindings)
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            for target in targets:
                for child in ast.walk(target):
                    if isinstance(child, ast.Name):
                        bindings[child.id] = set(sources)

    references: dict[str, set[str]] = {}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Attribute):
            for source in _expression_modules(node.value, bindings):
                references.setdefault(source, set()).add(node.attr)
        elif isinstance(node, ast.Call):
            strings = {
                value.value
                for value in [*node.args, *(keyword.value for keyword in node.keywords)]
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            if strings:
                expressions = [node.func, *node.args, *(keyword.value for keyword in node.keywords)]
                sources = {
                    source
                    for expression in expressions
                    for source in _expression_modules(expression, bindings)
                }
                for source in sources:
                    references.setdefault(source, set()).update(strings)
    return references


def _imported_module_bindings(  # noqa: C901
    tree: ast.Module,
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
) -> dict[str, set[str]]:
    """Map names imported by a consumer to their source helper modules."""
    bindings: dict[str, set[str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known_modules:
                    local = alias.asname or alias.name
                    bindings[local] = {alias.name}
        elif isinstance(node, ast.ImportFrom):
            source = resolve_import_source(package_parts, node.level, node.module)
            if source is not None:
                for alias in node.names:
                    if alias.name == "*" and source in known_modules:
                        for symbol in known_modules[source].symbols:
                            bindings[symbol.name] = {source}
                    elif alias.name != "*":
                        submodule = f"{source}.{alias.name}"
                        imported = (
                            submodule
                            if submodule in known_modules
                            else source
                            if source in known_modules
                            else None
                        )
                        if imported is not None:
                            bindings[alias.asname or alias.name] = {imported}
    return bindings


def _expression_modules(
    node: ast.AST,
    bindings: Mapping[str, set[str]],
) -> set[str]:
    dotted = _dotted_name(node) if isinstance(node, ast.expr) else None
    if dotted is not None:
        parts = dotted.split(".")
        for index in range(len(parts), 0, -1):
            sources = bindings.get(".".join(parts[:index]))
            if sources is not None:
                return set(sources)

    return {
        source
        for child in ast.iter_child_nodes(node)
        for source in _expression_modules(child, bindings)
    }


def _references_by_module(modules: Mapping[str, Module]) -> dict[str, set[str]]:
    """Map each referenced name to the modules that mention it."""
    references: dict[str, set[str]] = {}
    for module_name, module in modules.items():
        for name in _referenced_names(module):
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
    parts = dotted.split(".")
    for index in range(len(parts), 0, -1):
        prefix = ".".join(parts[:index])
        resolved = aliases.get(prefix)
        if resolved is not None:
            suffix = ".".join(parts[index:])
            return f"{resolved}.{suffix}" if suffix else resolved
    return dotted


def _decorator_aliases(  # noqa: C901
    tree: ast.Module,
    before_lineno: int,
) -> dict[str, str]:
    """Return canonical module-level names used by decorator expressions."""
    aliases = dict(_BUILTIN_DECORATORS)

    for node in tree.body:
        if node.lineno >= before_lineno:
            break
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
            aliases[node.name] = f"{_LOCAL_DECORATOR_PREFIX}.{node.name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for child in ast.walk(target):
                    if isinstance(child, ast.Name):
                        aliases[child.id] = f"{_LOCAL_DECORATOR_PREFIX}.{child.id}"
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
