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
uv run pre-commit install
```

## Run Tests

```bash
uv run pytest  # enforces 100% coverage
```

## Code Quality

```bash
uv run pre-commit run --all-files
uv build
```

Install the Git hook once after setup:

```bash
uv run pre-commit install
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
