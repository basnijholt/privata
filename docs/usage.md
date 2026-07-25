---
icon: lucide/terminal
---

# Usage

## Command

```bash
privata <project-root>
```

The command scans production Python source roots under `<project-root>`.
It uses `tach.toml` `source_roots` when present, otherwise prefers `src/`, otherwise scans the project root while ignoring tests, virtualenvs, build output, docs output, and hidden tooling directories.

## Pre-Commit

Add Privata to another repository's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.1.2
    hooks:
      - id: privata
```

The hook runs `privata .` once per commit, so it checks the repository as a whole instead of only the changed files.

For a less strict setup, use the manual hook:

```yaml
repos:
  - repo: https://github.com/basnijholt/privata
    rev: v0.1.2
    hooks:
      - id: privata-manual
```

Then run it on demand:

```bash
pre-commit run --hook-stage manual privata-manual --all-files
```

## Public Symbols

Privata reports top-level public symbols that are not imported from another production module:

```python
def helper() -> int:
    return 1
```

If `helper` is only used inside its own module, Privata reports it as a candidate for `_helper`.

## Public Methods

Privata also reports public methods that no other production module refers to:

```python
class Service:
    def run(self) -> int:
        return self.helper()

    def helper(self) -> int:
        return 1
```

If another module calls `Service().run()` but nothing outside `service.py` mentions `helper`, Privata reports `helper` as a candidate for `_helper`.
A method counts as used when another production module accesses the name as an attribute, such as `service.helper()`, or names it in a string literal, such as `getattr(service, "helper")`.
As with top-level symbols, use inside the defining module does not keep a method public, and use from tests does not either.

The name of a method can belong to something Privata cannot see, such as a framework base class or a registry, so the check only inspects plain classes and undecorated methods.
Privata skips:

- classes with a base class other than `object`, since the base may define the method contract
- classes with class keywords such as `metaclass=`
- classes with decorators other than `@dataclass` and `@final`
- private classes, classes listed in `__all__`, classes re-exported by package `__init__.py`, and classes exposed through entry points or a Tach interface
- classes nested inside functions or other classes
- dunder methods and methods that are already private
- methods carrying any decorator other than `@property`, `@staticmethod`, `@classmethod`, `@cached_property`, `@cache`, `@lru_cache`, and `@final`

The last rule is what keeps route handlers, Pydantic validators, pytest fixtures, and Celery tasks out of the report: those methods are registered under their current name by a decorator.

## Private Module Imports

Private modules are modules whose dotted path contains a private segment:

```text
package._internal
package.feature._runtime
```

Those modules can be imported from inside their owning package subtree.
Imports from outside that subtree are reported.

## Private Symbol Imports

Private top-level symbols are functions, classes, variables, or type aliases whose names begin with a single underscore:

```python
class _RuntimeService:
    pass
```

Imports of those names from another production module are reported.
Tests are ignored, so tests can still import internals without making them public.

## What Counts As Public Use

The following keep a symbol public:

- another module under a production source root imports the symbol, or refers to the method name
- a package `__init__.py` re-exports the symbol
- a literal `__all__` includes the symbol
- `pyproject.toml` lists the symbol as a console or GUI script entry point
- a shell script or Dockerfile launches the symbol as a Uvicorn app
- `tach.toml` exposes the symbol through a `[[interfaces]]` entry

Imports from tests do not count.

## Export Validation

When a module declares a literal `__all__`, Privata validates that it is exact.
Names listed in `__all__` must be bound by the module.
Public top-level bindings must be listed in `__all__`.
Use underscore-prefixed imports or helpers for implementation details that should not be exported.

## Suppressing Findings

Add a `# privata: ignore` comment to any import line to suppress that specific finding:

```python
from pkg._internal import helper  # privata: ignore
import pkg._internal  # privata: ignore
from pkg.impl import _Service  # privata: ignore
```

The same comment works on a `def` line to keep a public method out of the report:

```python
class Service:
    def run(self) -> int:  # privata: ignore
        return 1
```

The comment suppresses only the finding on that line. Other issues in the same file are still reported.

In multi-line imports, each imported name is reported on its own line, so put the comment on the line of the name you want to suppress.
A comment on the `from ...` header line only suppresses findings about the imported module itself, not the names inside the parentheses:

```python
from pkg.impl import (
    _Service,  # privata: ignore
    _Helper,  # still reported
)
```

## Framework Exceptions

Privata skips common framework-owned names:

- FastAPI route handlers and related request/response models
- Typer command callbacks
- framework app/router objects created with `FastAPI`, `APIRouter`, or `Typer`
- module-level `logger`

These names are often public by framework convention even when they are not imported from another production module.
