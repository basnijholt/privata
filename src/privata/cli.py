"""Command-line interface for Privata."""

from __future__ import annotations

from privata._checker import main as _checker_main


def main() -> int:
    """Run the Privata module privacy checker."""
    return _checker_main()


if __name__ == "__main__":
    raise SystemExit(main())
