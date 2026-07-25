"""Source-order reference attribution for co-located test helpers."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from privata._methods._bindings import (
    apply_import_bindings,
    apply_import_from_bindings,
    bind_names,
    bind_targets,
    copy_bindings,
    definitely_nonempty,
    expression_modules,
    match_bound_names,
    merge_states,
    replace_binding,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from privata._models import Module


def referenced_names_by_module(
    module: Module,
    known_modules: Mapping[str, Module],
) -> dict[str, set[str]]:
    """Return referenced names attributed to imported modules."""
    if module.tree is None:
        return {}

    references: dict[str, set[str]] = {}
    _collect_scoped_references(
        module.tree.body,
        {},
        module.package_parts,
        known_modules,
        references,
    )
    return references


def _collect_scoped_references(  # noqa: C901, PLR0912, PLR0913, PLR0915
    statements: list[ast.stmt],
    bindings: dict[str, set[str]],
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
    references: dict[str, set[str]],
    deferred_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    invoked_functions: set[int] | None = None,
) -> None:
    """Collect references while applying bindings in source order within each scope."""
    functions = {} if deferred_functions is None else deferred_functions
    invoked = set() if invoked_functions is None else invoked_functions
    defined_here: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in statements:
        if isinstance(node, ast.Import):
            apply_import_bindings(node, known_modules, bindings, functions)
        elif isinstance(node, ast.ImportFrom):
            apply_import_from_bindings(
                node,
                package_parts,
                known_modules,
                bindings,
                functions,
            )
        elif isinstance(node, ast.Assign):
            _scan_expression(
                node.value,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )
            sources = expression_modules(node.value, bindings)
            bind_targets(node.targets, sources, bindings, functions)
        elif isinstance(node, ast.AnnAssign):
            ann_sources: set[str] = set()
            if node.value is not None:
                _scan_expression(
                    node.value,
                    functions,
                    invoked,
                    bindings,
                    package_parts,
                    known_modules,
                    references,
                )
                ann_sources = expression_modules(node.value, bindings)
            bind_targets([node.target], ann_sources, bindings, functions)
        elif isinstance(node, ast.AugAssign):
            _scan_expression(
                node,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )
            sources = expression_modules(node.target, bindings) | expression_modules(
                node.value,
                bindings,
            )
            bind_targets([node.target], sources, bindings, functions)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = [
                *node.decorator_list,
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
                *(
                    argument.annotation
                    for argument in [
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                        node.args.vararg,
                        node.args.kwarg,
                    ]
                    if argument is not None and argument.annotation is not None
                ),
                node.returns,
            ]
            for expression in signature:
                if expression is not None:
                    _scan_expression(
                        expression,
                        functions,
                        invoked,
                        bindings,
                        package_parts,
                        known_modules,
                        references,
                    )
            replace_binding(node.name, set(), bindings, functions)
            functions[node.name] = node
            defined_here.append(node)
        elif isinstance(node, ast.ClassDef):
            for expression in [
                *node.decorator_list,
                *node.bases,
                *(keyword.value for keyword in node.keywords),
            ]:
                _scan_expression(
                    expression,
                    functions,
                    invoked,
                    bindings,
                    package_parts,
                    known_modules,
                    references,
                )
            class_bindings = copy_bindings(bindings)
            _collect_scoped_references(
                node.body,
                class_bindings,
                package_parts,
                known_modules,
                references,
                dict(functions),
                invoked,
            )
            replace_binding(node.name, set(), bindings, functions)
        elif isinstance(node, ast.If):
            _scan_expression(
                node.test,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )
            merge_states(
                bindings,
                functions,
                [
                    _collect_branch(
                        node.body,
                        bindings,
                        functions,
                        invoked,
                        package_parts,
                        known_modules,
                        references,
                    ),
                    _collect_branch(
                        node.orelse,
                        bindings,
                        functions,
                        invoked,
                        package_parts,
                        known_modules,
                        references,
                    ),
                ],
            )
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _scan_expression(
                node.iter,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )
            loop_bindings = copy_bindings(bindings)
            loop_functions = dict(functions)
            bind_targets(
                [node.target],
                expression_modules(node.iter, bindings),
                loop_bindings,
                loop_functions,
            )
            _collect_scoped_references(
                node.body,
                loop_bindings,
                package_parts,
                known_modules,
                references,
                loop_functions,
                invoked,
            )
            states = [(loop_bindings, loop_functions)]
            if not definitely_nonempty(node.iter):
                states.append((copy_bindings(bindings), dict(functions)))
            if node.orelse:
                states = [
                    _collect_branch(
                        node.orelse,
                        state_bindings,
                        state_functions,
                        invoked,
                        package_parts,
                        known_modules,
                        references,
                    )
                    for state_bindings, state_functions in states
                ]
            merge_states(bindings, functions, states)
        elif isinstance(node, ast.While):
            _scan_expression(
                node.test,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )
            states = [
                _collect_branch(
                    node.body,
                    bindings,
                    functions,
                    invoked,
                    package_parts,
                    known_modules,
                    references,
                ),
                (copy_bindings(bindings), dict(functions)),
            ]
            if node.orelse:
                states = [
                    _collect_branch(
                        node.orelse,
                        state_bindings,
                        state_functions,
                        invoked,
                        package_parts,
                        known_modules,
                        references,
                    )
                    for state_bindings, state_functions in states
                ]
            merge_states(bindings, functions, states)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                _scan_expression(
                    item.context_expr,
                    functions,
                    invoked,
                    bindings,
                    package_parts,
                    known_modules,
                    references,
                )
                if item.optional_vars is not None:
                    bind_targets(
                        [item.optional_vars],
                        expression_modules(item.context_expr, bindings),
                        bindings,
                        functions,
                    )
            _collect_scoped_references(
                node.body,
                bindings,
                package_parts,
                known_modules,
                references,
                functions,
                invoked,
            )
        elif isinstance(node, ast.Try):
            normal_state = _collect_branch(
                node.body,
                bindings,
                functions,
                invoked,
                package_parts,
                known_modules,
                references,
            )
            if node.orelse:
                normal_state = _collect_branch(
                    node.orelse,
                    normal_state[0],
                    normal_state[1],
                    invoked,
                    package_parts,
                    known_modules,
                    references,
                )
            states = [normal_state]
            for handler in node.handlers:
                handler_bindings = copy_bindings(bindings)
                handler_functions = dict(functions)
                if handler.type is not None:
                    _scan_expression(
                        handler.type,
                        handler_functions,
                        invoked,
                        handler_bindings,
                        package_parts,
                        known_modules,
                        references,
                    )
                if handler.name is not None:
                    replace_binding(
                        handler.name,
                        set(),
                        handler_bindings,
                        handler_functions,
                    )
                _collect_scoped_references(
                    handler.body,
                    handler_bindings,
                    package_parts,
                    known_modules,
                    references,
                    handler_functions,
                    invoked,
                )
                states.append((handler_bindings, handler_functions))
            merge_states(bindings, functions, states)
            if node.finalbody:
                _collect_scoped_references(
                    node.finalbody,
                    bindings,
                    package_parts,
                    known_modules,
                    references,
                    functions,
                    invoked,
                )
        elif isinstance(node, ast.Match):
            _scan_expression(
                node.subject,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )
            states = [(copy_bindings(bindings), dict(functions))]
            for case in node.cases:
                case_bindings = copy_bindings(bindings)
                case_functions = dict(functions)
                bind_names(
                    match_bound_names(case.pattern),
                    expression_modules(node.subject, bindings),
                    case_bindings,
                    case_functions,
                )
                if case.guard is not None:
                    _scan_expression(
                        case.guard,
                        case_functions,
                        invoked,
                        case_bindings,
                        package_parts,
                        known_modules,
                        references,
                    )
                _collect_scoped_references(
                    case.body,
                    case_bindings,
                    package_parts,
                    known_modules,
                    references,
                    case_functions,
                    invoked,
                )
                states.append((case_bindings, case_functions))
            merge_states(bindings, functions, states)
        elif isinstance(node, ast.Delete):
            bind_targets(node.targets, set(), bindings, functions)
        else:
            _scan_expression(
                node,
                functions,
                invoked,
                bindings,
                package_parts,
                known_modules,
                references,
            )

    for function in defined_here:
        if functions.get(function.name) is function and id(function) not in invoked:
            _analyze_function(
                function,
                bindings,
                package_parts,
                known_modules,
                references,
            )


def _scan_expression(  # noqa: PLR0913
    root: ast.AST,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    invoked_functions: set[int],
    bindings: Mapping[str, set[str]],
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
    references: dict[str, set[str]],
) -> None:
    if isinstance(root, ast.Lambda):
        for default in [
            *root.args.defaults,
            *(default for default in root.args.kw_defaults if default is not None),
        ]:
            _scan_expression(
                default,
                functions,
                invoked_functions,
                bindings,
                package_parts,
                known_modules,
                references,
            )
        local_bindings = copy_bindings(bindings)
        local_functions = dict(functions)
        bind_names(
            {
                argument.arg
                for argument in [
                    *root.args.posonlyargs,
                    *root.args.args,
                    *root.args.kwonlyargs,
                    root.args.vararg,
                    root.args.kwarg,
                ]
                if argument is not None
            },
            set(),
            local_bindings,
            local_functions,
        )
        _scan_expression(
            root.body,
            local_functions,
            invoked_functions,
            local_bindings,
            package_parts,
            known_modules,
            references,
        )
        return

    if isinstance(root, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        local_bindings = copy_bindings(bindings)
        local_functions = dict(functions)
        for generator in root.generators:
            _scan_expression(
                generator.iter,
                local_functions,
                invoked_functions,
                local_bindings,
                package_parts,
                known_modules,
                references,
            )
            bind_targets(
                [generator.target],
                expression_modules(generator.iter, local_bindings),
                local_bindings,
                local_functions,
            )
            for condition in generator.ifs:
                _scan_expression(
                    condition,
                    local_functions,
                    invoked_functions,
                    local_bindings,
                    package_parts,
                    known_modules,
                    references,
                )
        results = [root.key, root.value] if isinstance(root, ast.DictComp) else [root.elt]
        for result in results:
            _scan_expression(
                result,
                local_functions,
                invoked_functions,
                local_bindings,
                package_parts,
                known_modules,
                references,
            )
        return

    if isinstance(root, ast.Call) and isinstance(root.func, ast.Name):
        function = functions.get(root.func.id)
        if function is not None:
            invoked_functions.add(id(function))
            _analyze_function(
                function,
                bindings,
                package_parts,
                known_modules,
                references,
            )

    _record_reference(root, bindings, references)
    for child in ast.iter_child_nodes(root):
        _scan_expression(
            child,
            functions,
            invoked_functions,
            bindings,
            package_parts,
            known_modules,
            references,
        )


def _analyze_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: Mapping[str, set[str]],
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
    references: dict[str, set[str]],
) -> None:
    local_bindings = {name: set(sources) for name, sources in bindings.items()}
    arguments = {
        argument.arg
        for argument in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
            function.args.vararg,
            function.args.kwarg,
        ]
        if argument is not None
    }
    local_bindings.update({name: set() for name in arguments})
    _collect_scoped_references(
        function.body,
        local_bindings,
        package_parts,
        known_modules,
        references,
    )


def _record_reference(
    node: ast.AST,
    bindings: Mapping[str, set[str]],
    references: dict[str, set[str]],
) -> None:
    if isinstance(node, ast.Attribute):
        for source in expression_modules(node.value, bindings):
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
                for source in expression_modules(expression, bindings)
            }
            for source in sources:
                references.setdefault(source, set()).update(strings)


def _collect_branch(  # noqa: PLR0913
    statements: list[ast.stmt],
    bindings: Mapping[str, set[str]],
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    invoked_functions: set[int],
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
    references: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    branch_bindings = copy_bindings(bindings)
    branch_functions = dict(functions)
    _collect_scoped_references(
        statements,
        branch_bindings,
        package_parts,
        known_modules,
        references,
        branch_functions,
        invoked_functions,
    )
    return branch_bindings, branch_functions
