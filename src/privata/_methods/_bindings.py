"""Binding-state primitives for test-helper reference analysis."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._imports import resolve_import_source
from privata._methods._ast import dotted_name as _dotted_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from privata._models import Module


def copy_bindings(bindings: Mapping[str, set[str]]) -> dict[str, set[str]]:
    return {name: set(sources) for name, sources in bindings.items()}


def merge_states(
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    states: list[tuple[dict[str, set[str]], dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]],
) -> None:
    binding_names = {name for state, _ in states for name in state}
    bindings.clear()
    bindings.update(
        {
            name: {source for state, _ in states for source in state.get(name, set())}
            for name in binding_names
        },
    )

    function_names = {name for _, state in states for name in state}
    functions.clear()
    for name in function_names:
        candidates = [state.get(name) for _, state in states]
        first = candidates[0]
        if first is not None and all(candidate is first for candidate in candidates):
            functions[name] = first


def definitely_nonempty(node: ast.expr) -> bool:
    return isinstance(node, (ast.List, ast.Tuple, ast.Set)) and bool(node.elts)


def apply_import_bindings(
    node: ast.Import,
    known_modules: Mapping[str, Module],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    for alias in node.names:
        if alias.name in known_modules:
            local = alias.asname or alias.name
            replace_binding(local, {alias.name}, bindings, functions)


def apply_import_from_bindings(
    node: ast.ImportFrom,
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    source = resolve_import_source(package_parts, node.level, node.module)
    if source is not None:
        for alias in node.names:
            if alias.name == "*" and source in known_modules:
                for symbol in known_modules[source].symbols:
                    replace_binding(symbol.name, {source}, bindings, functions)
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
                    replace_binding(
                        alias.asname or alias.name,
                        {imported},
                        bindings,
                        functions,
                    )


def bind_targets(
    targets: list[ast.expr],
    sources: set[str],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    for target in targets:
        bind_names(_target_names(target), sources, bindings, functions)


def bind_names(
    names: set[str],
    sources: set[str],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    for name in names:
        replace_binding(name, sources, bindings, functions)


def replace_binding(
    name: str,
    sources: set[str],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    descendants = [bound for bound in bindings if bound == name or bound.startswith(f"{name}.")]
    for bound in descendants:
        del bindings[bound]
    bindings[name] = set(sources)
    functions.pop(name, None)


def _target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        dotted = _dotted_name(target)
        return {dotted} if dotted is not None else set()
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for element in target.elts for name in _target_names(element)}
    return set()


def match_bound_names(pattern: ast.pattern) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.add(node.rest)
    return names


def expression_modules(
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
        for source in expression_modules(child, bindings)
    }
