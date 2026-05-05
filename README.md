# Privata

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/basnijholt/privata/actions/workflows/ci.yml/badge.svg)](https://github.com/basnijholt/privata/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/privata.svg)](https://pypi.org/project/privata/)
[![Python Versions](https://img.shields.io/pypi/pyversions/privata.svg)](https://pypi.org/project/privata/)
[![Docs](https://img.shields.io/badge/docs-basnijholt.github.io%2Fprivata-blue)](https://basnijholt.github.io/privata/)

Keep Python module interfaces intentional.

Privata scans a `src/` layout Python project and reports public top-level symbols that are only used inside their own module.
It also reports imports of private modules from outside their owning package subtree.
Test imports do not count, so tests can still reach internals without forcing those internals to stay public.

## Install

```bash
uv tool install privata
```

For local development:

```bash
uv sync --extra dev
```

## Usage

Run Privata from a project root:

```bash
privata .
```

Example output:

```text
Found 2 public symbols that could be made private:

  src/example/service.py:12: function `helper`
  src/example/service.py:21: class `InternalState`

Found 1 private module imports outside their package subtree:

  src/example/api.py:3: imports private module `example.worker._runtime`
```

If the project is clean:

```text
No module privacy issues found.
```

## What Privata Checks

- Public top-level functions, classes, variables, and type aliases in `src/`.
- Whether those symbols are imported by another production module under `src/`.
- Whether private modules such as `pkg._internal` are imported outside their containing package subtree.
- Console entry points in `pyproject.toml`.
- Uvicorn entry points in shell scripts and Dockerfiles.
- Symbols exported through package `__init__.py` and `__all__`.
- Tach `[[interfaces]]` entries, when `tach.toml` is present.

Privata intentionally ignores imports from `tests/`.
If only tests import a symbol, Privata treats that symbol as private.

## Development

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy src tests
uv run --extra dev ty check
uv build
```
