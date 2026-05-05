# Repository Guidelines

Privata is a small Python package for AST-based module privacy checks.

## Development

- Use `uv sync --extra dev --group docs` for a full development environment.
- Run `uv run pytest` for tests.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, and `uv run ty check` before submitting changes.
- Use `uv build` to verify packaging.

## Structure

- `src/privata/_checker.py` contains the checker implementation.
- `src/privata/cli.py` exposes the console script.
- `tests/test_checker.py` contains focused behavior tests.
- `docs/` contains the Zensical documentation site.

## Style

- Keep runtime dependencies minimal.
- Prefer standard-library parsing and typed data structures.
- Preserve the rule that test imports do not count when deciding whether a symbol should remain public.
