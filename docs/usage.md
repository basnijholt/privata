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

## Private Module Imports

Private modules are modules whose dotted path contains a private segment:

```text
package._internal
package.feature._runtime
```

Those modules can be imported from inside their owning package subtree.
Imports from outside that subtree are reported.

## What Counts As Public Use

The following keep a symbol public:

- another module under a production source root imports the symbol
- a package `__init__.py` re-exports the symbol
- a literal `__all__` includes the symbol
- `pyproject.toml` lists the symbol as a console or GUI script entry point
- a shell script or Dockerfile launches the symbol as a Uvicorn app
- `tach.toml` exposes the symbol through a `[[interfaces]]` entry

Imports from tests do not count.

## Framework Exceptions

Privata skips common framework-owned names:

- FastAPI route handlers and related request/response models
- Typer command callbacks
- framework app/router objects created with `FastAPI`, `APIRouter`, or `Typer`
- module-level `logger`

These names are often public by framework convention even when they are not imported from another production module.
