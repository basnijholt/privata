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
_BUILTIN_ALIASES = {
    "classmethod": "builtins.classmethod",
    "object": "builtins.object",
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


def collect_package_reexports(modules: Mapping[str, Module]) -> set[tuple[str, str]]:
    """Return classes exposed by runtime imports in package modules."""
    reexports: set[tuple[str, str]] = set()
    for module in modules.values():
        if module.tree is None or module.path.name != "__init__.py":
            continue
        for node in _runtime_module_imports(module.tree.body):
            source = resolve_import_source(module.package_parts, node.level, node.module)
            if source is None or source not in modules:
                continue
            if any(alias.name == "*" for alias in node.names):
                reexports.update(
                    (source, symbol.name)
                    for symbol in modules[source].symbols
                    if not symbol.name.startswith("_")
                )
            defined = {symbol.name for symbol in modules[source].symbols}
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "*" or (local.startswith("_") and local not in module.exports):
                    continue
                if f"{source}.{alias.name}" not in modules and alias.name in defined:
                    reexports.add((source, alias.name))
    return reexports


def _runtime_module_imports(  # noqa: C901
    statements: list[ast.stmt],
) -> Iterator[ast.ImportFrom]:
    """Yield imports that can create module-level runtime bindings."""
    for node in statements:
        if isinstance(node, ast.ImportFrom):
            yield node
        elif isinstance(node, ast.If):
            guard = _type_checking_guard(node.test)
            if guard is True:
                yield from _runtime_module_imports(node.orelse)
            elif guard is False:
                yield from _runtime_module_imports(node.body)
            else:
                yield from _runtime_module_imports(node.body)
                yield from _runtime_module_imports(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _runtime_module_imports(node.body)
            yield from _runtime_module_imports(node.orelse)
            yield from _runtime_module_imports(node.finalbody)
            for handler in node.handlers:
                yield from _runtime_module_imports(handler.body)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            yield from _runtime_module_imports(node.body)
            yield from _runtime_module_imports(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _runtime_module_imports(node.body)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _runtime_module_imports(case.body)


def _type_checking_guard(node: ast.expr) -> bool | None:
    """Return whether a conventional guard disables its body at runtime."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        guarded = _type_checking_guard(node.operand)
        return None if guarded is None else not guarded
    if _dotted_name(node) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
        return True
    return None


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
            _apply_import_bindings(node, known_modules, bindings, functions)
        elif isinstance(node, ast.ImportFrom):
            _apply_import_from_bindings(
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
            sources = _expression_modules(node.value, bindings)
            _bind_targets(node.targets, sources, bindings, functions)
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
                ann_sources = _expression_modules(node.value, bindings)
            _bind_targets([node.target], ann_sources, bindings, functions)
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
            sources = _expression_modules(node.target, bindings) | _expression_modules(
                node.value,
                bindings,
            )
            _bind_targets([node.target], sources, bindings, functions)
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
            _replace_binding(node.name, set(), bindings, functions)
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
            class_bindings = _copy_bindings(bindings)
            _collect_scoped_references(
                node.body,
                class_bindings,
                package_parts,
                known_modules,
                references,
                dict(functions),
                invoked,
            )
            _replace_binding(node.name, set(), bindings, functions)
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
            _merge_states(
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
            loop_bindings = _copy_bindings(bindings)
            loop_functions = dict(functions)
            _bind_targets(
                [node.target],
                _expression_modules(node.iter, bindings),
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
            if not _definitely_nonempty(node.iter):
                states.append((_copy_bindings(bindings), dict(functions)))
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
            _merge_states(bindings, functions, states)
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
                (_copy_bindings(bindings), dict(functions)),
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
            _merge_states(bindings, functions, states)
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
                    _bind_targets(
                        [item.optional_vars],
                        _expression_modules(item.context_expr, bindings),
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
                handler_bindings = _copy_bindings(bindings)
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
                    _replace_binding(
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
            _merge_states(bindings, functions, states)
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
            states = [(_copy_bindings(bindings), dict(functions))]
            for case in node.cases:
                case_bindings = _copy_bindings(bindings)
                case_functions = dict(functions)
                _bind_names(
                    _match_bound_names(case.pattern),
                    _expression_modules(node.subject, bindings),
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
            _merge_states(bindings, functions, states)
        elif isinstance(node, ast.Delete):
            _bind_targets(node.targets, set(), bindings, functions)
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
        local_bindings = _copy_bindings(bindings)
        local_functions = dict(functions)
        _bind_names(
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
        local_bindings = _copy_bindings(bindings)
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
            _bind_targets(
                [generator.target],
                _expression_modules(generator.iter, local_bindings),
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


def _collect_branch(  # noqa: PLR0913
    statements: list[ast.stmt],
    bindings: Mapping[str, set[str]],
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
    invoked_functions: set[int],
    package_parts: tuple[str, ...],
    known_modules: Mapping[str, Module],
    references: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    branch_bindings = _copy_bindings(bindings)
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


def _copy_bindings(bindings: Mapping[str, set[str]]) -> dict[str, set[str]]:
    return {name: set(sources) for name, sources in bindings.items()}


def _merge_states(
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


def _definitely_nonempty(node: ast.expr) -> bool:
    return isinstance(node, (ast.List, ast.Tuple, ast.Set)) and bool(node.elts)


def _apply_import_bindings(
    node: ast.Import,
    known_modules: Mapping[str, Module],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    for alias in node.names:
        if alias.name in known_modules:
            local = alias.asname or alias.name
            _replace_binding(local, {alias.name}, bindings, functions)


def _apply_import_from_bindings(
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
                    _replace_binding(symbol.name, {source}, bindings, functions)
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
                    _replace_binding(
                        alias.asname or alias.name,
                        {imported},
                        bindings,
                        functions,
                    )


def _bind_targets(
    targets: list[ast.expr],
    sources: set[str],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    for target in targets:
        _bind_names(_target_names(target), sources, bindings, functions)


def _bind_names(
    names: set[str],
    sources: set[str],
    bindings: dict[str, set[str]],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    for name in names:
        _replace_binding(name, sources, bindings, functions)


def _replace_binding(
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


def _match_bound_names(pattern: ast.pattern) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.add(node.rest)
    return names


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
    aliases = dict(decorator_aliases)
    protected_methods = _protected_method_nodes(class_node)
    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _update_aliases(aliases, node)
            continue
        checkable = _is_checkable_method(node, aliases)
        aliases[node.name] = f"{_LOCAL_DECORATOR_PREFIX}.{node.name}"
        if not checkable:
            continue
        if id(node) in protected_methods:
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


def _protected_method_nodes(class_node: ast.ClassDef) -> set[int]:  # noqa: C901
    """Return methods whose class binding is consumed or replaced later."""
    current_methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    protected: set[int] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            header = [
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
            for expression in header:
                if expression is not None:
                    for name in _loaded_names(expression):
                        if name in current_methods:
                            protected.add(id(current_methods[name]))
            previous = current_methods.get(node.name)
            if previous is not None:
                protected.add(id(previous))
            current_methods[node.name] = node
            continue
        for name in _loaded_names(node):
            if name in current_methods:
                protected.add(id(current_methods[name]))
        for name in _bound_names(node):
            previous = current_methods.pop(name.split(".", 1)[0], None)
            if previous is not None:
                protected.add(id(previous))
    return protected


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
    if any(_resolved_name(base, decorator_aliases) != "builtins.object" for base in node.bases):
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
    return _resolved_name(target, aliases)


def _resolved_name(node: ast.expr, aliases: Mapping[str, str]) -> str | None:
    dotted = _dotted_name(node)
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


def _decorator_aliases(
    tree: ast.Module,
    before_lineno: int,
) -> dict[str, str]:
    """Return canonical module-level names used by decorator expressions."""
    aliases = dict(_BUILTIN_ALIASES)

    for node in tree.body:
        if node.lineno >= before_lineno:
            break
        _update_aliases(aliases, node)
    return aliases


def _update_aliases(aliases: dict[str, str], node: ast.stmt) -> None:
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
    else:
        for name in _bound_names(node):
            aliases[name] = f"{_LOCAL_DECORATOR_PREFIX}.{name}"


class _BoundNameCollector(ast.NodeVisitor):
    """Collect bindings without descending into nested scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            dotted = _dotted_name(node)
            if dotted is not None:
                self.names.add(dotted)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names.add(node.rest)
        self.generic_visit(node)


def _bound_names(node: ast.stmt) -> set[str]:
    collector = _BoundNameCollector()
    collector.visit(node)
    return collector.names


class _LoadedNameCollector(ast.NodeVisitor):
    """Collect class-scope reads without entering nested scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        del node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        del node

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        del node

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node


def _loaded_names(node: ast.AST) -> set[str]:
    collector = _LoadedNameCollector()
    collector.visit(node)
    return collector.names


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None
