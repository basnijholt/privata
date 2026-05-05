---
icon: lucide/git-pull-request
---

# Contributing

Contributions are welcome.

## Development Setup

```bash
git clone https://github.com/basnijholt/privata.git
cd privata
uv sync --extra dev --group docs
```

## Run Tests

```bash
uv run pytest
```

## Code Quality

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run ty check
uv build
```

The repository also uses pre-commit:

```bash
uv run pre-commit run --all-files
```

## Build Docs

```bash
uv run zensical build
```

The generated site is written to `site/`.

## Project Structure

```text
src/privata/
├── __init__.py   Public package API
├── __main__.py   python -m privata entry point
├── _checker.py   AST-based privacy analysis
└── cli.py        Console script wrapper
```

## Release

Releases are published from GitHub Releases through trusted publishing.
Create a release tag such as `v0.1.0`; the `release.yml` workflow builds and uploads to PyPI.
