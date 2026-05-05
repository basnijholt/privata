"""Tests for ``privata``."""

from __future__ import annotations

import runpy
from typing import TYPE_CHECKING

import pytest

from privata import find_private_candidates, find_private_module_imports
from privata._checker import Module, collect_private_module_imports, find_cross_imports, main
from privata.cli import main as cli_main

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _symbols(project_root: Path) -> set[tuple[str, str]]:
    return {(symbol.module, symbol.name) for symbol in find_private_candidates(project_root)}


def _private_module_imports(project_root: Path) -> set[tuple[str, str]]:
    return {
        (issue.module, issue.imported_by) for issue in find_private_module_imports(project_root)
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
    monkeypatch.setattr("sys.argv", ["privata"])

    assert main() == 1
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project without Python source files is clean instead of requiring src/."""
    monkeypatch.setattr("sys.argv", ["privata", str(tmp_path)])

    assert main() == 0
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
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr("sys.argv", ["privata", str(tmp_path)])

    assert main() == 0
    output = capsys.readouterr()
    assert output.out == "No module privacy issues found.\n"
    assert output.err == ""


def test_cli_reports_private_imports_without_symbol_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr("sys.argv", ["privata", str(tmp_path)])

    assert main() == 1
    output = capsys.readouterr()
    assert "Found 1 private module imports outside their package subtree:" in output.out
    assert "src/pkg/two/public.py:1: imports private module `pkg.one._internal`" in output.out


def test_cli_separates_symbol_and_private_import_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr("sys.argv", ["privata", str(tmp_path)])

    assert main() == 1
    output = capsys.readouterr()
    assert "function `local_helper`\n\nFound 1 private module imports" in output.out


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

    with pytest.raises(SystemExit) as checker_exit:
        runpy.run_module("privata._checker", run_name="__main__")
    assert checker_exit.value.code == 0

    with pytest.raises(SystemExit) as cli_exit:
        runpy.run_module("privata.cli", run_name="__main__")
    assert cli_exit.value.code == 0
