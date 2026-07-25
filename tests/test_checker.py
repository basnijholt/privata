"""Tests for ``privata``."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from privata import (
    find_export_issues,
    find_method_candidates,
    find_module_collisions,
    find_private_candidates,
    find_private_module_imports,
    find_private_symbol_imports,
)
from privata._exports import collect_export_issues
from privata._imports import (
    collect_private_module_imports,
    collect_private_symbol_imports,
    find_cross_imports,
)
from privata._methods import (
    collect_method_candidates,
    referenced_names_by_module,
)
from privata._models import Module
from privata.cli import main as cli_main


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _symbols(project_root: Path) -> set[tuple[str, str]]:
    return {(symbol.module, symbol.name) for symbol in find_private_candidates(project_root)}


def _methods(project_root: Path) -> set[tuple[str, str, str]]:
    return {
        (method.module, method.class_name, method.name)
        for method in find_method_candidates(project_root)
    }


def _private_module_imports(project_root: Path) -> set[tuple[str, str]]:
    return {
        (issue.module, issue.imported_by) for issue in find_private_module_imports(project_root)
    }


def _private_symbol_imports(project_root: Path) -> set[tuple[str, str, str]]:
    return {
        (issue.module, issue.name, issue.imported_by)
        for issue in find_private_symbol_imports(project_root)
    }


def _export_issues(project_root: Path) -> set[tuple[str, str, str]]:
    return {(issue.module, issue.name, issue.kind) for issue in find_export_issues(project_root)}


def _module_collisions(project_root: Path) -> dict[str, list[str]]:
    return {
        collision.module: [path.relative_to(project_root).as_posix() for path in collision.paths]
        for collision in find_module_collisions(project_root)
    }


def test_fastapi_route_functions_and_models_are_skipped(tmp_path: Path) -> None:
    """FastAPI route handlers and request/response models should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class RequestModel(BaseModel):
    value: int

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "health") not in symbols
    assert ("pkg.api", "RequestModel") not in symbols
    assert ("pkg.api", "router") not in symbols
    assert ("pkg.api", "local_helper") in symbols


def test_fastapi_related_type_aliases_and_derived_models_are_skipped(tmp_path: Path) -> None:
    """Names only referenced from route signatures/decorators should be skipped."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Annotated

router = APIRouter()

class BasePayload(BaseModel):
    name: str

class ExtendedPayload(BasePayload):
    age: int

RoomFilter = Annotated[str | None, Query(default=None)]

@router.get("/items", response_model=ExtendedPayload)
async def list_items(room_id: RoomFilter = None) -> ExtendedPayload:
    return ExtendedPayload(name="a", age=1)
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "ExtendedPayload") not in symbols
    assert ("pkg.api", "RoomFilter") not in symbols
    assert ("pkg.api", "list_items") not in symbols


def test_typer_callbacks_are_skipped(tmp_path: Path) -> None:
    """Typer command callbacks should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
import typer

app = typer.Typer()

@app.command()
def run() -> None:
    pass

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "app") not in symbols
    assert ("pkg.cli", "run") not in symbols
    assert ("pkg.cli", "local_helper") in symbols


def test_logger_variable_is_ignored(tmp_path: Path) -> None:
    """Module-level logger should be ignored by privacy checks."""
    _write(
        tmp_path / "src" / "pkg" / "mod.py",
        """
from logging import getLogger

logger = getLogger(__name__)

def local_helper() -> int:
    logger.info("x")
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.mod", "logger") not in symbols
    assert ("pkg.mod", "local_helper") in symbols


def test_pyproject_console_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Console-script entrypoints in pyproject.toml should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
def main() -> int:
    return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "example"
version = "0.1.0"
scripts.example = "pkg.cli:main"
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "main") not in symbols


def test_fastapi_include_router_dependency_symbols_are_skipped(tmp_path: Path) -> None:
    """FastAPI dependency callbacks used via include_router should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Depends, FastAPI

app = FastAPI()
router = APIRouter()

async def verify_user() -> dict[str, str]:
    return {"id": "1"}

app.include_router(router, dependencies=[Depends(verify_user)])

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "verify_user") not in symbols
    assert ("pkg.api", "local_helper") in symbols


def test_shell_uvicorn_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Uvicorn entrypoints in shell scripts should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "server.py",
        """
def build_app() -> object:
    return object()

asgi = build_app()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "run-server.sh",
        """
#!/usr/bin/env bash
exec uvicorn pkg.server:asgi --host 0.0.0.0 --port 8000
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.server", "asgi") not in symbols


def test_package_init_reexports_count_as_cross_module_imports(tmp_path: Path) -> None:
    """Symbols re-exported from package ``__init__`` are production imports."""
    _write(
        tmp_path / "src" / "pkg" / "features" / "types.py",
        """
PUBLIC_VALUE = "value"

def public_helper() -> str:
    return PUBLIC_VALUE

def local_helper() -> str:
    return "local"
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "features" / "__init__.py",
        """
from .types import PUBLIC_VALUE, public_helper

__all__ = ["PUBLIC_VALUE", "public_helper"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.features.types", "PUBLIC_VALUE") not in symbols
    assert ("pkg.features.types", "public_helper") not in symbols
    assert ("pkg.features.types", "local_helper") in symbols


def test_tach_interface_exposed_symbols_are_skipped(tmp_path: Path) -> None:
    """Tach interface exposure marks a symbol as public even without src imports."""
    _write(
        tmp_path / "src" / "pkg" / "runtime.py",
        """
class RuntimeFacade:
    pass

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = ["src"]

[[interfaces]]
from = ["pkg.runtime"]
expose = ["RuntimeFacade"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.runtime", "RuntimeFacade") not in symbols
    assert ("pkg.runtime", "local_helper") in symbols


def test_private_module_imported_within_same_package_is_ignored(tmp_path: Path) -> None:
    """Private modules can be imported from within their own package subtree."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "one" / "public.py",
        """
from ._internal import helper

VALUE = helper()
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_private_module_imported_from_other_package_is_reported(tmp_path: Path) -> None:
    """Private modules imported outside their package subtree should be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import _internal

VALUE = _internal.VALUE
""".strip()
        + "\n",
    )

    private_module_imports = _private_module_imports(tmp_path)
    assert ("pkg.one._internal", "pkg.two.public") in private_module_imports


def test_cli_reports_private_symbol_imports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Private top-level symbols imported by production modules should be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "write_coordinator.py",
        """
class _EventCacheWriteCoordinator:
    pass

def local_helper() -> None:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "runtime_support.py",
        """
from pkg.write_coordinator import _EventCacheWriteCoordinator
""".strip()
        + "\n",
    )

    assert cli_main([str(tmp_path)]) == 1

    output = capsys.readouterr()
    assert "Found 1 private symbol imports from production modules:" in output.out
    assert (
        "src/pkg/runtime_support.py:1: imports private symbol "
        "`pkg.write_coordinator._EventCacheWriteCoordinator`"
    ) in output.out
    assert "function `local_helper`\n\nFound 1 private symbol imports" in output.out


def test_private_symbol_imports_are_reported(tmp_path: Path) -> None:
    """Private symbol import findings include the source symbol and consumer."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        """
class _PrivateService:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .producer import _PrivateService
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == {
        ("pkg.producer", "_PrivateService", "pkg.consumer"),
    }


def test_private_symbol_self_import_is_ignored(tmp_path: Path) -> None:
    """Self imports do not count as private symbol boundary crossings."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        """
from .producer import _PrivateService

class _PrivateService:
    pass
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == set()


def test_private_module_imported_only_by_tests_is_ignored(tmp_path: Path) -> None:
    """Only imports from production source roots count for private module detection."""
    _write(
        tmp_path / "src" / "pkg" / "_internal.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_private.py",
        """
from pkg._internal import helper

assert helper() == 1
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_cli_defaults_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running privata without arguments scans the current directory."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    monkeypatch.chdir(tmp_path)

    assert cli_main([]) == 1
    output = capsys.readouterr()
    assert "src/pkg/module.py:1: function `helper`" in output.out
    assert output.err == ""


def test_collect_modules_skips_invalid_and_cache_files(tmp_path: Path) -> None:
    """Invalid Python and cache files should not affect scanning."""
    _write(tmp_path / "src" / "__init__.py", "")
    _write(
        tmp_path / "src" / "single.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(tmp_path / "src" / "broken.py", "def broken(:\n")
    _write(tmp_path / "src" / "__pycache__" / "cached.py", "CACHED = 1\n")

    symbols = _symbols(tmp_path)
    assert ("single", "VALUE") in symbols
    assert ("broken", "broken") not in symbols
    assert ("cached", "CACHED") not in symbols


def test_annotated_assignments_type_aliases_and_unpacking_are_collected(
    tmp_path: Path,
) -> None:
    """Assignment forms should be discovered as public symbols."""
    _write(
        tmp_path / "src" / "pkg" / "types.py",
        """
Name: type = str
Alias = int
PairAlias: type = tuple[str, str]
FIRST, (SECOND, _ignored) = (1, (2, 3))
app = make_app()()
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.types", "Name") in symbols
    assert ("pkg.types", "Alias") in symbols
    assert ("pkg.types", "PairAlias") in symbols
    assert ("pkg.types", "FIRST") in symbols
    assert ("pkg.types", "SECOND") in symbols
    assert ("pkg.types", "app") in symbols


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
def test_type_statement_is_collected_on_supported_python(tmp_path: Path) -> None:
    """PEP 695 type statements are collected when the parser supports them."""
    _write(
        tmp_path / "src" / "pkg" / "aliases.py",
        """
type UserId = int
""".strip()
        + "\n",
    )

    assert ("pkg.aliases", "UserId") in _symbols(tmp_path)


def test_framework_callback_signature_names_are_skipped(tmp_path: Path) -> None:
    """Framework callback signature annotations and defaults mark names public."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter, Depends

router = APIRouter()

class Item:
    pass

class Params:
    pass

def fallback() -> Item:
    return Item()

@router.post("/items")
def create_item(*items: Item, item: Item = fallback(), **params: Params) -> Item:
    return item
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.api", "Item") not in symbols
    assert ("pkg.api", "Params") not in symbols
    assert ("pkg.api", "fallback") not in symbols
    assert ("pkg.api", "create_item") not in symbols


def test_non_framework_decorators_and_dynamic_bases_are_handled(tmp_path: Path) -> None:
    """Plain decorators and dynamic bases should not crash name analysis."""
    _write(
        tmp_path / "src" / "pkg" / "models.py",
        """
def decorator(func):
    return func

def make_base():
    return object()

@decorator
def helper() -> int:
    return 1

class Dynamic(make_base().Base):
    pass
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.models", "decorator") in symbols
    assert ("pkg.models", "make_base") in symbols
    assert ("pkg.models", "helper") in symbols
    assert ("pkg.models", "Dynamic") in symbols


def test_non_literal_all_does_not_hide_symbols(tmp_path: Path) -> None:
    """A dynamic ``__all__`` is ignored because it cannot be trusted statically."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
EXPORTED = "exported"
NAMES = ["EXPORTED"]
__all__ = ["EXPORTED", *NAMES]
""".strip()
        + "\n",
    )

    assert ("pkg.exports", "EXPORTED") in _symbols(tmp_path)


def test_literal_all_hides_exported_symbols(tmp_path: Path) -> None:
    """A literal ``__all__`` marks listed symbols public."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
EXPORTED = "exported"
__all__ = ["EXPORTED"]
""".strip()
        + "\n",
    )

    assert ("pkg.exports", "EXPORTED") not in _symbols(tmp_path)


def test_literal_all_reports_unknown_and_missing_public_exports(tmp_path: Path) -> None:
    """Literal ``__all__`` should be exact for public local top-level bindings."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
from __future__ import annotations
import json as json_module
from dataclasses import dataclass

__all__ = ["Exported", "MISSING"]

@dataclass
class Exported:
    value: str

async def async_helper() -> None:
    pass

LOCAL = json_module.dumps({"x": 1})
_PRIVATE = 1
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == {
        ("pkg.exports", "MISSING", "unknown"),
        ("pkg.exports", "LOCAL", "missing"),
        ("pkg.exports", "async_helper", "missing"),
    }


def test_literal_all_accepts_reexports_and_try_fallbacks(tmp_path: Path) -> None:
    """Common package export patterns should validate when ``__all__`` is complete."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
class PublicThing:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from .module import PublicThing

try:
    from ._version import __version__
except ImportError:
    __version__ = "0.0.0"

__all__ = ["PublicThing", "__version__"]
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_does_not_require_package_init_imports(tmp_path: Path) -> None:
    """Package imports are valid explicit exports but are not mandatory exports."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
class PublicThing:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from .module import PublicThing

__all__ = []
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_does_not_require_logger_binding(tmp_path: Path) -> None:
    """Module-level loggers are implementation details by convention."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["public"]

logger = object()

def public() -> None:
    pass
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_does_not_require_regular_module_imports(tmp_path: Path) -> None:
    """Imported dependencies are not local public API that must be listed in ``__all__``."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import json

__all__ = ["Exported"]

@dataclass
class Exported:
    path: Path
    value: Any
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_accepts_imported_regular_module_exports(tmp_path: Path) -> None:
    """A regular module may still explicitly export an imported binding."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
from .types import PublicType

__all__ = ["PublicType"]
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


def test_literal_all_reports_private_explicit_exports(tmp_path: Path) -> None:
    """Literal ``__all__`` should not export private bindings."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["_private_helper"]

def _private_helper() -> None:
    pass
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == {("pkg.exports", "_private_helper", "private")}


def test_dynamic_all_is_not_validated(tmp_path: Path) -> None:
    """Non-literal ``__all__`` forms are ignored by export validation."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
PUBLIC = 1
NAMES = ["PUBLIC"]
__all__ = ["PUBLIC", *NAMES]
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == set()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
def test_literal_all_checks_annotated_assignments_and_type_aliases(tmp_path: Path) -> None:
    """Annotated assignments and PEP 695 type aliases are public bindings."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["Name"]

Name: type = str
type UserId = int
""".strip()
        + "\n",
    )

    assert _export_issues(tmp_path) == {("pkg.exports", "UserId", "missing")}


def test_export_validation_ignores_unparsed_modules(tmp_path: Path) -> None:
    """Modules without parsed ASTs should not affect export validation."""
    module = Module("pkg.empty", tmp_path / "empty.py", ("pkg",), tree=None)

    assert collect_export_issues({"pkg.empty": module}) == []


def test_string_all_does_not_hide_symbols(tmp_path: Path) -> None:
    """A string ``__all__`` is malformed and should not hide symbols."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
EXPORTED = "exported"
__all__ = "EXPORTED"
""".strip()
        + "\n",
    )

    assert ("pkg.exports", "EXPORTED") in _symbols(tmp_path)


def test_unsupported_assignment_targets_are_ignored(tmp_path: Path) -> None:
    """Attribute assignment targets are not top-level public symbol definitions."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
class Holder:
    pass

holder = Holder()
holder.value = 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.module", "Holder") in symbols
    assert ("pkg.module", "holder") in symbols
    assert ("pkg.module", "value") not in symbols


def test_star_imports_submodule_imports_and_bad_relatives_count_correctly(
    tmp_path: Path,
) -> None:
    """Cross-import detection should handle star, submodule, and invalid relative imports."""
    _write(
        tmp_path / "src" / "pkg" / "source.py",
        """
VALUE = 1
OTHER = 2
LOCAL = 3
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "submod.py",
        """
THING = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .source import *
from . import submod
from ...missing import nope

USED = VALUE + submod.THING
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.source", "VALUE") not in symbols
    assert ("pkg.source", "OTHER") not in symbols
    assert ("pkg.source", "LOCAL") not in symbols
    assert ("pkg.submod", "THING") not in symbols


def test_attribute_access_with_non_name_base_is_ignored(tmp_path: Path) -> None:
    """Only imported module aliases should mark module attributes as externally used."""
    _write(
        tmp_path / "src" / "pkg" / "source.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
def factory():
    import pkg.source
    return pkg.source

USED = factory().VALUE
""".strip()
        + "\n",
    )

    assert ("pkg.source", "VALUE") in _symbols(tmp_path)


def test_private_module_import_edge_cases(tmp_path: Path) -> None:
    """Private import detection handles self imports and invalid relatives."""
    _write(
        tmp_path / "src" / "pkg" / "_internal.py",
        """
import pkg._internal
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "public.py",
        """
from ...missing import _internal
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_private_module_import_helpers_ignore_unparsed_modules(tmp_path: Path) -> None:
    """Internal helpers should tolerate modules without parsed ASTs."""
    module = Module("pkg.empty", tmp_path / "empty.py", ("pkg",), tree=None)

    assert find_cross_imports({"pkg.empty": module}) == set()
    assert collect_private_module_imports({"pkg.empty": module}) == []
    assert collect_private_symbol_imports({"pkg.empty": module}) == []


def test_annotated_framework_constructor_is_skipped(tmp_path: Path) -> None:
    """Annotated framework constructor assignments should not be flagged."""
    _write(
        tmp_path / "src" / "pkg" / "api.py",
        """
from fastapi import APIRouter

router: APIRouter = APIRouter()
""".strip()
        + "\n",
    )

    assert ("pkg.api", "router") not in _symbols(tmp_path)


def test_pyproject_ignores_malformed_entrypoints(tmp_path: Path) -> None:
    """Only string entrypoints with module:symbol shape are public."""
    _write(
        tmp_path / "src" / "pkg" / "cli.py",
        """
def main() -> int:
    return 0

def gui() -> int:
    return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "example"
scripts = "not-a-table"

[project.gui-scripts]
good = "pkg.cli:gui"
bad_shape = "pkg.cli"
bad_type = 1
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.cli", "gui") not in symbols
    assert ("pkg.cli", "main") in symbols


def test_scripts_directory_uvicorn_entrypoint_is_skipped(tmp_path: Path) -> None:
    """Uvicorn entrypoints under scripts/ should be honored."""
    _write(
        tmp_path / "src" / "pkg" / "server.py",
        """
app = object()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "scripts" / "serve.sh",
        """
uvicorn pkg.server:app
""".strip()
        + "\n",
    )

    assert ("pkg.server", "app") not in _symbols(tmp_path)


def test_tach_ignores_malformed_interfaces(tmp_path: Path) -> None:
    """Malformed Tach interface entries should not mark symbols public."""
    _write(
        tmp_path / "src" / "pkg" / "runtime.py",
        """
class RuntimeFacade:
    pass

class OtherFacade:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
[[interfaces]]
from = "pkg.runtime"
expose = ["RuntimeFacade"]

[[interfaces]]
from = [1, "pkg.runtime"]
expose = [2, "OtherFacade"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.runtime", "RuntimeFacade") in symbols
    assert ("pkg.runtime", "OtherFacade") not in symbols


def test_cli_reports_no_src_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project without Python source files is clean instead of requiring src/."""
    assert cli_main([str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


def test_project_root_is_scanned_when_src_directory_is_absent(tmp_path: Path) -> None:
    """Projects without src/ should still be scanned from the project root."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_module.py",
        """
from pkg.module import helper
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_root_level_test_files_are_ignored_in_project_root_fallback(tmp_path: Path) -> None:
    """Root-level pytest modules should not count as production imports."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "test_module.py",
        """
from pkg.module import helper
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "module_test.py",
        """
from pkg.module import helper as imported_helper
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_root_test_files_are_ignored_when_scanning_project_root(tmp_path: Path) -> None:
    """Root-level test files should not keep production symbols public."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "test_module.py",
        """
from pkg.module import helper
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_tach_source_roots_define_scanned_roots(tmp_path: Path) -> None:
    """Tach source_roots should control which roots are scanned."""
    _write(
        tmp_path / "lib" / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "pkg" / "ignored.py",
        """
def ignored() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = ["lib"]
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("pkg.module", "helper") in symbols
    assert ("pkg.ignored", "ignored") not in symbols


def test_malformed_tach_source_roots_fall_back_to_project_layout(tmp_path: Path) -> None:
    """Malformed Tach source_roots should not break source discovery."""
    _write(
        tmp_path / "pkg" / "module.py",
        """
def helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = "pkg"
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)

    _write(
        tmp_path / "tach.toml",
        """
source_roots = [1, "missing"]
""".strip()
        + "\n",
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_cli_reports_clean_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project with no public candidates reports cleanly."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def _helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


def test_cli_reports_private_imports_without_symbol_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Private import findings are printed even when no public symbols are found."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
_VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import _internal
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Found 1 private module imports outside their package subtree:" in output.out
    assert "src/pkg/two/public.py:1: imports private module `pkg.one._internal`" in output.out


def test_cli_reports_export_issues_without_symbol_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Literal ``__all__`` mismatches are printed as their own finding section."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["MISSING"]
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Found 1 __all__ export issues:" in output.out
    assert "src/pkg/exports.py:1: __all__ exports unknown name `MISSING`" in output.out


def test_cli_reports_private_all_exports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Private names in ``__all__`` should be printed as private export issues."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["_private_helper"]

def _private_helper() -> None:
    pass
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "src/pkg/exports.py:1: __all__ exports private name `_private_helper`" in output.out


def test_cli_separates_symbol_and_private_import_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blank line separates symbol and private import sections."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        """
_VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import _internal

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "function `local_helper`\n\nFound 1 private module imports" in output.out


def test_cli_separates_export_findings_from_previous_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blank line separates export findings from earlier finding sections."""
    _write(
        tmp_path / "src" / "pkg" / "exports.py",
        """
__all__ = ["MISSING"]

def local_helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "function `local_helper`\n\nFound 2 __all__ export issues" in output.out


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_cli_wrapper_and_module_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The installed CLI wrapper and Python module entrypoints call the checker."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def _helper() -> int:
    return 1
""".strip()
        + "\n",
    )
    monkeypatch.setattr("sys.argv", ["privata", str(tmp_path)])

    assert cli_main() == 0
    assert "No module privacy issues found." in capsys.readouterr().out

    with pytest.raises(SystemExit) as package_exit:
        runpy.run_module("privata", run_name="__main__")
    assert package_exit.value.code == 0

    with pytest.raises(SystemExit) as cli_exit:
        runpy.run_module("privata.cli", run_name="__main__")
    assert cli_exit.value.code == 0


def test_cli_uses_argparse_for_help(capsys: pytest.CaptureFixture[str]) -> None:
    """The console wrapper should expose argparse help without running checks."""
    with pytest.raises(SystemExit) as cli_exit:
        cli_main(["--help"])

    assert cli_exit.value.code == 0
    output = capsys.readouterr()
    assert output.out.startswith("usage: privata")
    assert "project-root" in output.out
    assert "No module privacy issues found." not in output.out
    assert output.err == ""


def test_cli_accepts_explicit_argv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The argparse wrapper accepts an argv sequence for tests and embedding."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        """
def _helper() -> int:
    return 1
""".strip()
        + "\n",
    )

    assert cli_main([str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_checker_module_is_not_runtime_entrypoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The internal checker module should not also act as a CLI entrypoint."""
    runpy.run_module("privata._checker", run_name="__main__")

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_privata_ignore_suppresses_bare_private_module_import(tmp_path: Path) -> None:
    """A # privata: ignore comment on a bare import statement suppresses the finding."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        "VALUE = 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        "import pkg.one._internal  # privata: ignore\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_privata_ignore_suppresses_from_private_module_import(tmp_path: Path) -> None:
    """A # privata: ignore comment on a from-import suppresses a private module finding."""
    _write(
        tmp_path / "src" / "pkg" / "one" / "_internal.py",
        "VALUE = 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        "from pkg.one import _internal  # privata: ignore\n",
    )

    assert _private_module_imports(tmp_path) == set()


def test_privata_ignore_suppresses_private_symbol_import(tmp_path: Path) -> None:
    """A # privata: ignore comment on a from-import suppresses a private symbol finding."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        "class _PrivateService:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        "from .producer import _PrivateService  # privata: ignore\n",
    )

    assert _private_symbol_imports(tmp_path) == set()


def test_privata_ignore_suppresses_one_multiline_private_symbol_import(
    tmp_path: Path,
) -> None:
    """In multi-line imports, # privata: ignore applies to the alias line."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        """
class _IgnoredService:
    pass

class _ReportedService:
    pass
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .producer import (
    _IgnoredService,  # privata: ignore
    _ReportedService,
)
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == {
        ("pkg.producer", "_ReportedService", "pkg.consumer"),
    }


def test_privata_ignore_suppresses_one_multiline_private_submodule_import(
    tmp_path: Path,
) -> None:
    """In multi-line from-imports, # privata: ignore applies per private submodule alias."""
    _write(tmp_path / "src" / "pkg" / "one" / "_ignored.py", "VALUE = 1\n")
    _write(tmp_path / "src" / "pkg" / "one" / "_reported.py", "VALUE = 2\n")
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import (
    _ignored,  # privata: ignore
    _reported,
)
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == {
        ("pkg.one._reported", "pkg.two.public"),
    }


def test_privata_ignore_on_multiline_header_keeps_private_symbol_findings(
    tmp_path: Path,
) -> None:
    """In multi-line imports, header comments do not suppress alias findings."""
    _write(
        tmp_path / "src" / "pkg" / "producer.py",
        "class _PrivateService:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
from .producer import (  # privata: ignore
    _PrivateService,
)
""".strip()
        + "\n",
    )

    assert _private_symbol_imports(tmp_path) == {
        ("pkg.producer", "_PrivateService", "pkg.consumer"),
    }


def test_privata_ignore_on_multiline_header_keeps_private_submodule_findings(
    tmp_path: Path,
) -> None:
    """In multi-line from-imports, header comments do not suppress private submodules."""
    _write(tmp_path / "src" / "pkg" / "one" / "_internal.py", "VALUE = 1\n")
    _write(
        tmp_path / "src" / "pkg" / "two" / "public.py",
        """
from pkg.one import (  # privata: ignore
    _internal,
)
""".strip()
        + "\n",
    )

    assert _private_module_imports(tmp_path) == {
        ("pkg.one._internal", "pkg.two.public"),
    }


def test_plain_import_chained_attribute_access_is_detected(tmp_path: Path) -> None:
    """import pkg.mod followed by pkg.mod.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module

result = provider.module.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_plain_import_deeply_chained_attribute_access_is_detected(tmp_path: Path) -> None:
    """import a.b.c.mod followed by a.b.c.mod.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import pkg.sub.leaf

result = pkg.sub.leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_aliased_plain_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """import pkg.mod as m followed by m.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module as m

result = m.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_aliased_deep_plain_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """import a.b.c.mod as m followed by m.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import pkg.sub.leaf as leaf

result = leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_from_submodule_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import mod followed by mod.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module

result = module.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_from_submodule_aliased_import_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import mod as m followed by m.Symbol should count as cross-module usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module as m

result = m.get_value()
""".strip()
        + "\n",
    )

    assert ("provider.module", "get_value") not in _symbols(tmp_path)


def test_plain_import_chained_class_and_variable_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """import pkg.mod followed by pkg.mod.SomeClass and pkg.mod.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module

x = provider.module.SomeClass()
y = provider.module.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_aliased_import_class_and_variable_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """import pkg.mod as m followed by m.SomeClass and m.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
import provider.module as m

x = m.SomeClass()
y = m.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_from_import_class_and_variable_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import mod followed by mod.SomeClass and mod.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module

x = module.SomeClass()
y = module.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_from_aliased_import_class_and_variable_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """from pkg import mod as m followed by m.SomeClass and m.GLOBAL should count as usage."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
class SomeClass:
    pass

GLOBAL_VALUE = 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from provider import module as m

x = m.SomeClass()
y = m.GLOBAL_VALUE
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "SomeClass") not in symbols
    assert ("provider.module", "GLOBAL_VALUE") not in symbols


def test_aliased_subpackage_import_deep_attribute_access_is_detected(tmp_path: Path) -> None:
    """from pkg import sub followed by sub.mod.Symbol should count as cross-module usage."""
    _write(tmp_path / "src" / "pkg" / "sub" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from pkg import sub

result = sub.leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_aliased_subpackage_aliased_import_deep_attribute_access_is_detected(
    tmp_path: Path,
) -> None:
    """from pkg import sub as s followed by s.mod.Symbol should count as cross-module usage."""
    _write(tmp_path / "src" / "pkg" / "sub" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "sub" / "leaf.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
from pkg import sub as s

result = s.leaf.get_value()
""".strip()
        + "\n",
    )

    assert ("pkg.sub.leaf", "get_value") not in _symbols(tmp_path)


def test_chained_attribute_on_call_result_base_is_ignored(tmp_path: Path) -> None:
    """Attribute chains rooted in a call result should not be treated as module access."""
    _write(
        tmp_path / "src" / "pkg" / "source.py",
        """
VALUE = 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "consumer.py",
        """
def factory():
    pass

USED = factory().sub.VALUE
""".strip()
        + "\n",
    )

    assert ("pkg.source", "VALUE") in _symbols(tmp_path)


def test_unimported_dotted_attribute_access_is_ignored(tmp_path: Path) -> None:
    """Dotted names should only count as module usage when backed by an import."""
    _write(
        tmp_path / "src" / "provider" / "module.py",
        """
def get_value() -> int:
    return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "consumer" / "module.py",
        """
result = provider.module.get_value()
""".strip()
        + "\n",
    )

    symbols = _symbols(tmp_path)
    assert ("provider.module", "get_value") in symbols


def test_privata_is_clean_under_its_own_rules() -> None:
    """Privata should pass its own public-symbol check."""
    project_root = Path(__file__).resolve().parents[1]

    assert _symbols(project_root) == set()


def test_test_source_root_consumers_count_for_publicness(tmp_path: Path) -> None:
    """Symbols in a test source root used by co-located test files should not be flagged."""
    _write(
        tmp_path / "tests" / "something.py",
        "def get_value() -> int:\n    return 42\n",
    )
    # snake_case test file
    _write(
        tmp_path / "tests" / "inner_folder" / "test_blah.py",
        "import something\n\ndef test_value() -> None:\n    assert something.get_value() == 42\n",
    )
    # camelCase testFoo style
    _write(
        tmp_path / "tests" / "inner_folder" / "testHelper.py",
        "import something\n\ndef testValue() -> None:\n    something.get_value()\n",
    )
    # camelCase fooTest style
    _write(
        tmp_path / "tests" / "inner_folder" / "helperTest.py",
        "import something\n",
    )
    # test file inside a hidden subdir → skipped as consumer
    _write(
        tmp_path / "tests" / ".hidden" / "test_noise.py",
        "import something\n",
    )
    # test file with broken syntax → skipped as consumer
    _write(
        tmp_path / "tests" / "test_broken.py",
        "def bad(:\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )
    assert ("something", "get_value") not in _symbols(tmp_path)


def test_test_source_root_consumers_do_not_count_for_production_modules(
    tmp_path: Path,
) -> None:
    """Test source roots must not make production symbols public."""
    _write(
        tmp_path / "src" / "pkg" / "module.py",
        "def helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_module.py",
        "from pkg.module import helper\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )

    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_camelcase_test_files_do_not_count_as_consumers(tmp_path: Path) -> None:
    """camelCase test filenames should not keep production symbols public."""
    _write(
        tmp_path / "pkg" / "module.py",
        "def helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "testModule.py",
        "from pkg.module import helper\n",
    )
    _write(
        tmp_path / "moduleTest.py",
        "from pkg.module import helper\n",
    )
    assert ("pkg.module", "helper") in _symbols(tmp_path)


def test_test_source_root_symbols_without_consumers_remain_flagged(tmp_path: Path) -> None:
    """Symbols in a test source root with no test consumers are still private candidates."""
    _write(
        tmp_path / "tests" / "something.py",
        "def get_value() -> int:\n    return 42\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )
    assert ("something", "get_value") in _symbols(tmp_path)


def test_test_source_root_partially_consumed_symbols(tmp_path: Path) -> None:
    """In a test source root, used symbols are cleared while unused ones remain flagged."""
    _write(
        tmp_path / "tests" / "something.py",
        "def get_value() -> int:\n    return 42\n\ndef get_bar() -> int:\n    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        "import something\n\ndef test_value() -> None:\n    assert something.get_value() == 42\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )
    symbols = _symbols(tmp_path)
    assert ("something", "get_value") not in symbols
    assert ("something", "get_bar") in symbols


def test_module_name_collision_across_source_roots(tmp_path: Path) -> None:
    """Two source roots defining the same module name should be reported."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "utils.py",
        "def test_helper() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )
    assert _module_collisions(tmp_path) == {"utils": ["src/utils.py", "tests/utils.py"]}


def test_module_and_package_collision_in_same_root(tmp_path: Path) -> None:
    """A module next to a same-named package is ambiguous and should be reported."""
    _write(
        tmp_path / "src" / "pkg.py",
        "def helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        "",
    )
    assert _module_collisions(tmp_path) == {"pkg": ["src/pkg/__init__.py", "src/pkg.py"]}


def test_distinct_module_names_have_no_collisions(tmp_path: Path) -> None:
    """Distinct module names across roots should not be reported, and test files never count."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "helper.py",
        "def test_helper() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tests" / "utils_test.py",
        "def utils() -> int:\n    return 3\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )
    assert _module_collisions(tmp_path) == {}


def test_duplicate_source_roots_are_not_collisions(tmp_path: Path) -> None:
    """Listing the same source root twice must not report a file as colliding with itself."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "src"]\n',
    )
    assert _module_collisions(tmp_path) == {}


def test_cli_reports_module_collisions_before_other_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Collisions are printed as their own section ahead of symbol candidates."""
    _write(
        tmp_path / "src" / "utils.py",
        "def production_helper() -> int:\n    return 1\n",
    )
    _write(
        tmp_path / "tests" / "utils.py",
        "def unused() -> int:\n    return 2\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["src", "tests"]\n',
    )
    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Found 1 module names defined by multiple files" in output.out
    assert "module `utils` is defined by: src/utils.py, tests/utils.py" in output.out
    assert "public symbols that could be made private" in output.out
    assert output.out.index("defined by multiple files") < output.out.index("could be made private")


def test_module_local_public_method_is_flagged(tmp_path: Path) -> None:
    """A public method that only its own module uses should be reported."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    label = "service"

    def __init__(self) -> None:
        self.count = 0

    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1

    async def async_helper(self) -> int:
        return 2
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "helper"),
        ("pkg.service", "Service", "async_helper"),
    }


def test_method_used_from_another_module_is_not_flagged(tmp_path: Path) -> None:
    """Attribute access from another production module keeps a method public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )

    assert _methods(tmp_path) == set()


def test_method_named_in_a_string_literal_elsewhere_is_not_flagged(tmp_path: Path) -> None:
    """A getattr-style string reference in another module keeps a method public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        """
from pkg.service import Service


def start() -> int:
    return getattr(Service(), "run")()
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_private_and_dunder_methods_are_not_flagged(tmp_path: Path) -> None:
    """Methods that are already private, and dunder methods, are never candidates."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def __init__(self) -> None:
        self.value = 1

    def __repr__(self) -> str:
        return "Service()"

    def _helper(self) -> int:
        return self.value
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_methods_of_private_classes_are_not_flagged(tmp_path: Path) -> None:
    """A private class is already internal, so its method names are not reported."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class _Service:\n    def run(self) -> int:\n        return 1\n",
    )

    assert _methods(tmp_path) == set()


def test_methods_of_subclasses_are_not_flagged(tmp_path: Path) -> None:
    """A base class Privata cannot inspect may require the method name, so skip the class."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from abc import ABC


class Service(ABC):
    def run(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_methods_of_classes_with_class_keywords_are_not_flagged(tmp_path: Path) -> None:
    """Class keyword arguments such as metaclass= signal an external contract."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service(metaclass=type):
    def run(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_explicit_object_base_is_still_checked(tmp_path: Path) -> None:
    """An explicit object base adds no external contract."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service(object):\n    def run(self) -> int:\n        return 1\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "run")}


def test_shadowed_object_base_is_not_checked(tmp_path: Path) -> None:
    """A name spelled object may still refer to an external base class."""
    _write(
        tmp_path / "src" / "framework.py",
        "class Base:\n    pass\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from framework import Base as object


class Service(object):
    def run(self) -> int:
        return 1
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_rename_safe_decorators_are_still_checked(tmp_path: Path) -> None:
    """Decorators that only change binding behavior do not protect a method name."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import functools


class Service:
    @property
    def value(self) -> int:
        return 1

    @staticmethod
    def build() -> int:
        return 2

    @classmethod
    def create(cls) -> int:
        return 3

    @functools.cached_property
    def total(self) -> int:
        return 4
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "value"),
        ("pkg.service", "Service", "build"),
        ("pkg.service", "Service", "create"),
        ("pkg.service", "Service", "total"),
    }


def test_custom_decorators_with_safe_basenames_are_skipped(tmp_path: Path) -> None:
    """A trusted decorator basename does not make an unrelated decorator rename-safe."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import registry
import functools
import typing
from registry import final
from .registry import property

functools = registry
typing.final = registry.final
for functools in [registry]:
    pass


@registry.dataclass
class Registered:
    def run(self) -> int:
        return 1


class Service:
    @final
    def handle(self) -> int:
        return 2

    @property
    def value(self) -> int:
        return 3

    @functools.cache
    def cached(self) -> int:
        return 4

    @typing.final
    def finalized(self) -> int:
        return 5
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_conditionally_shadowed_safe_decorators_are_skipped(tmp_path: Path) -> None:
    """A binding inside control flow may replace a trusted decorator."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import functools

if ENABLE_CUSTOM:
    from registry import property
    functools.cache = registry.cache


class Service:
    @property
    def value(self) -> int:
        return 1

    @functools.cache
    def cached(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_compound_decorator_bindings_are_conservative(tmp_path: Path) -> None:
    """Every binding form can shadow a trusted decorator name."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
async def property():
    return None


class classmethod:
    pass


staticmethod = lambda function: function

try:
    pass
except Exception as cached:
    pass

match VALUE:
    case {"decorator": property, **rest}:
        pass
    case [*items]:
        pass


class Service:
    @property
    def value(self) -> int:
        return 1

    @classmethod
    def create(cls) -> int:
        return 2

    @staticmethod
    def build() -> int:
        return 3

    def plain(self) -> int:
        return 4


class Rebound:
    def overwritten(self) -> int:
        return 5

    overwritten = 5


class Aliased:
    def run(self) -> int:
        return 6

    if ENABLE:
        def nested():
            return run

        async def async_nested():
            return run

        class Nested:
            pass

        callback = lambda: run

    alias = run
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "plain")}


def test_class_local_decorator_bindings_resolve_in_source_order(tmp_path: Path) -> None:
    """A class-local assignment only shadows later decorator expressions."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import registry


class Service:
    @property
    def builtin_value(self) -> int:
        return 1

    property = registry.property

    @property
    def registered_value(self) -> int:
        return 2

    @external
    def externally_registered(self) -> int:
        return 3

    def plain(self) -> int:
        return 4
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {
        ("pkg.service", "Service", "builtin_value"),
        ("pkg.service", "Service", "plain"),
    }


def test_unknown_method_decorators_are_skipped(tmp_path: Path) -> None:
    """A decorator may register the method under its current name, so leave it alone."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
import celery

registry = {}


class Service:
    @celery.task
    def run(self) -> int:
        return 1

    @registry["handler"]
    def handle(self) -> int:
        return 2

    @property
    def value(self) -> int:
        return 3

    @value.setter
    def value(self, new_value: int) -> None:
        self._value = new_value
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_unknown_class_decorators_are_skipped(tmp_path: Path) -> None:
    """A decorated class may be registered elsewhere, while dataclasses stay checkable."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
from dataclasses import dataclass

import registry


@registry.register
class Registered:
    def run(self) -> int:
        return 1


@dataclass
class Plain:
    value: int = 0

    def compute(self) -> int:
        return self.value
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Plain", "compute")}


def test_exported_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """Methods of a class listed in __all__ are part of the module interface."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
__all__ = ["Service"]


class Service:
    def run(self) -> int:
        return 1


class Internal:
    def run_internal(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Internal", "run_internal")}


def test_package_reexported_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """A class re-exported from a package module is part of the package interface."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        'from .service import Service\n\n__all__ = ["Service"]\n',
    )

    assert _methods(tmp_path) == set()


def test_chained_package_reexported_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """Each package hop preserves the defining class as part of the public interface."""
    _write(
        tmp_path / "src" / "pkg" / "sub" / "impl.py",
        "class Thing:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "sub" / "__init__.py",
        'from .impl import Thing\n\n__all__ = ["Thing"]\n',
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        'from .sub import Thing\n\n__all__ = ["Thing"]\n',
    )

    assert _methods(tmp_path) == set()


def test_package_local_imports_do_not_count_as_reexports(tmp_path: Path) -> None:
    """Function-local and TYPE_CHECKING imports do not expose a class."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import Service


def build():
    from .service import Service

    return Service()
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "run")}


def test_package_control_flow_reexport_keeps_class_methods_public(tmp_path: Path) -> None:
    """A runtime package import inside fallback control flow remains a reexport."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
try:
    from .service import Service
except ImportError:
    from .service import Service
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_package_runtime_control_flow_reexports_are_detected(tmp_path: Path) -> None:
    """Runtime module-scope imports remain reexports across control-flow forms."""
    modules = {
        "starred": "Starred",
        "private": "Private",
        "runtime": "Runtime",
        "conditional": "Conditional",
        "fallback": "Fallback",
        "negated": "Negated",
        "iterated": "Iterated",
        "iter_else": "IterElse",
        "looped": "Looped",
        "loop_else": "LoopElse",
        "contextual": "Contextual",
        "matched": "Matched",
    }
    for module, class_name in modules.items():
        _write(
            tmp_path / "src" / "pkg" / f"{module}.py",
            f"class {class_name}:\n    def run(self) -> int:\n        return 1\n",
        )
    _write(
        tmp_path / "src" / "pkg" / "__init__.py",
        """
from typing import TYPE_CHECKING

from .starred import *
from .private import Private as _Private

if not TYPE_CHECKING:
    from .runtime import Runtime

if ENABLE:
    from .conditional import Conditional
else:
    from .fallback import Fallback

if not ENABLE:
    from .negated import Negated

for item in ITEMS:
    from .iterated import Iterated
else:
    from .iter_else import IterElse

while ENABLE:
    from .looped import Looped
else:
    from .loop_else import LoopElse

with manager():
    from .contextual import Contextual

match MODE:
    case _:
        from .matched import Matched
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.private", "Private", "run")}


def test_tach_interface_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """Methods of a class exposed through a Tach interface stay public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tach.toml",
        """
source_roots = ["src"]

[[interfaces]]
expose = ["Service"]
from = ["pkg.service"]
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_entrypoint_class_methods_are_not_flagged(tmp_path: Path) -> None:
    """A class used as a console script target keeps its methods public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pkg"\n\n[project.scripts]\npkg = "pkg.service:Service"\n',
    )

    assert _methods(tmp_path) == set()


def test_method_ignore_comment_suppresses_finding(tmp_path: Path) -> None:
    """A # privata: ignore comment on the def line suppresses the method finding."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def kept(self) -> int:  # privata: ignore
        return 1

    def flagged(self) -> int:
        return 2
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "flagged")}


def test_methods_of_nested_classes_are_not_flagged(tmp_path: Path) -> None:
    """Only top-level classes are inspected for method privacy."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
def build() -> type:
    class Nested:
        def run(self) -> int:
            return 1

    return Nested
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == set()


def test_test_source_root_test_files_certify_helper_methods(tmp_path: Path) -> None:
    """Test files certify methods of helper modules in their own test source root."""
    _write(
        tmp_path / "tests" / "something.py",
        """
class Helper:
    def get_value(self) -> int:
        return 42

    def get_unused(self) -> int:
        return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
import something


def test_value() -> None:
    assert getattr(something.Helper(), "get_value")() == 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("something", "Helper", "get_unused")}


def test_star_imported_test_helper_methods_are_certified(tmp_path: Path) -> None:
    """Star imports retain methods that co-located tests call."""
    _write(
        tmp_path / "tests" / "something.py",
        """
class Helper:
    def get_value(self) -> int:
        return 42

    def get_unused(self) -> int:
        return 0
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tests" / "test_blah.py",
        """
from something import *


def test_value() -> None:
    assert Helper().get_value() == 42
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("something", "Helper", "get_unused")}


def test_test_method_references_are_scoped_to_the_imported_helper(tmp_path: Path) -> None:
    """A test reference does not certify a same-named method in another helper."""
    _write(
        tmp_path / "tests" / "used_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "unused_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 2\n",
    )
    _write(
        tmp_path / "tests" / "test_used_helper.py",
        """
from unused_helper import Helper as UnusedHelper
from used_helper import Helper


def test_run() -> None:
    unused = UnusedHelper()
    used: Helper = Helper()
    assert unused
    assert used.run() == 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("unused_helper", "Helper", "run")}


def test_shadowed_test_helper_import_uses_the_last_binding(tmp_path: Path) -> None:
    """A later import replaces an earlier helper bound to the same local name."""
    _write(
        tmp_path / "tests" / "used_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "unused_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 2\n",
    )
    _write(
        tmp_path / "tests" / "test_used_helper.py",
        """
from unused_helper import Helper
from used_helper import Helper


def test_run() -> None:
    assert Helper().run() == 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("unused_helper", "Helper", "run")}


@pytest.mark.parametrize(
    "expression",
    [
        "(lambda Helper: Helper.run())(object())",
        "[Helper.run() for Helper in [object()]]",
        "[Helper for Helper in [object()] if Helper.run()]",
    ],
    ids=["lambda", "comprehension", "comprehension-filter"],
)
def test_expression_local_shadowing_does_not_certify_helper_method(
    tmp_path: Path,
    expression: str,
) -> None:
    """Expression-local names replace imported helper bindings."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        f"from helper import Helper\n\n{expression}\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("helper", "Helper", "run")}


def test_lambda_default_uses_outer_helper_binding(tmp_path: Path) -> None:
    """Lambda defaults are evaluated before parameter bindings exist."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        "from helper import Helper\n\ncallback = lambda value=Helper.run: value\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == set()


def test_interleaved_test_helper_bindings_preserve_source_order(tmp_path: Path) -> None:
    """An assignment before a later import does not erase the runtime-winning import."""
    _write(
        tmp_path / "tests" / "used_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "unused_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 2\n",
    )
    _write(
        tmp_path / "tests" / "test_used_helper.py",
        """
from unused_helper import Helper

Helper = object

def test_run() -> None:
    assert Helper().run() == 1


from used_helper import Helper
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("unused_helper", "Helper", "run")}


def test_module_invocation_uses_bindings_at_call_time(tmp_path: Path) -> None:
    """A direct module call uses bindings present before a later reimport."""
    _write(
        tmp_path / "tests" / "used_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "unused_helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 2\n",
    )
    _write(
        tmp_path / "tests" / "test_used_helper.py",
        """
from used_helper import Helper


def exercise() -> None:
    assert Helper().run() == 1


exercise()

from unused_helper import Helper
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("unused_helper", "Helper", "run")}


def test_conditional_test_helper_import_certifies_method(tmp_path: Path) -> None:
    """A helper imported and used inside one branch is still referenced."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
def test_run(enabled: bool) -> None:
    if enabled:
        from helper import Helper

        assert Helper().run() == 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == set()


def test_nonempty_loop_rebinding_replaces_test_helper_import(tmp_path: Path) -> None:
    """A definitely executed loop target replaces an imported helper binding."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
from helper import Helper


class Other:
    def run(self) -> int:
        return 2


for Helper in [Other]:
    pass

Helper().run()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("helper", "Helper", "run")}


def test_rebound_local_function_is_not_analyzed_at_call_site(tmp_path: Path) -> None:
    """A call after rebinding does not invoke the old local function."""
    _write(
        tmp_path / "tests" / "helper.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
from helper import Helper


def exercise() -> int:
    return Helper().run()


exercise = lambda: 0
exercise()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("helper", "Helper", "run")}


def test_rebinding_dotted_import_root_invalidates_descendants(tmp_path: Path) -> None:
    """Replacing a package binding invalidates imported dotted modules."""
    _write(
        tmp_path / "tests" / "helpers" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "tests" / "helpers" / "unused.py",
        "class Helper:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
import helpers.unused
from types import SimpleNamespace

helpers = SimpleNamespace(unused=object())
helpers.unused.run()
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == {("helpers.unused", "Helper", "run")}


def test_test_helper_compound_scopes_update_bindings(tmp_path: Path) -> None:
    """Compound statements process imports, targets, and references in scope."""
    method_names = [
        "while_call",
        "while_else_call",
        "for_else_call",
        "augmented_call",
        "with_call",
        "try_call",
        "except_call",
        "else_call",
        "finally_call",
        "match_call",
        "star_call",
    ]
    methods = "\n".join(
        f"    def {name}(self) -> int:\n        return 1\n" for name in method_names
    )
    _write(
        tmp_path / "tests" / "helper.py",
        f"class Helper:\n{methods}",
    )
    _write(
        tmp_path / "tests" / "test_helper.py",
        """
from helper import Helper

alias = Helper
alias += Helper
alias.augmented_call()


class Container(Helper, metaclass=Helper):
    helper = Helper()


def retained() -> int:
    return Helper().for_else_call()


for item in ITEMS:
    pass
else:
    retained()


while ENABLE:
    from helper import Helper as WhileHelper

    WhileHelper().while_call()
else:
    Helper().while_else_call()

with Helper() as managed:
    managed.with_call()

try:
    Helper().try_call()
except Helper as error:
    Helper().except_call()
else:
    Helper().else_call()
finally:
    Helper().finally_call()

match Helper():
    case {"item": matched, **rest} if Helper():
        matched.match_call()
    case [*items]:
        Helper().star_call()

(first, *remaining) = (Helper, [])
namespace.value = Helper
mapping["helper"] = Helper
del alias
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "tach.toml",
        'source_roots = ["tests"]\n',
    )

    assert _methods(tmp_path) == set()


def test_methods_used_only_by_tests_are_flagged(tmp_path: Path) -> None:
    """Test usage does not keep a production method public."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "class Service:\n    def run(self) -> int:\n        return 1\n",
    )
    _write(
        tmp_path / "tests" / "test_service.py",
        """
from pkg.service import Service


def test_run() -> None:
    assert Service().run()
""".strip()
        + "\n",
    )

    assert _methods(tmp_path) == {("pkg.service", "Service", "run")}


def test_method_collection_skips_modules_without_a_tree() -> None:
    """Modules that failed to parse contribute no methods and no references."""
    module = Module(name="pkg.mod", path=Path("pkg/mod.py"), package_parts=("pkg",))

    assert referenced_names_by_module(module, {}) == {}
    assert collect_method_candidates({"pkg.mod": module}) == []


def test_cli_reports_method_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Method candidates are printed in their own section after symbol candidates."""
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        """
class Service:
    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "src" / "pkg" / "app.py",
        "from pkg.service import Service\n\n\ndef start() -> int:\n    return Service().run()\n",
    )

    assert cli_main([str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Found 1 public methods that could be made private:" in output
    assert "src/pkg/service.py:5: method `Service.helper`" in output
    assert output.index("public symbols that could be made private") < output.index(
        "public methods that could be made private",
    )
