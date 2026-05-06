"""Command-line interface for Privata."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from privata._checker import check_project

if TYPE_CHECKING:
    from collections.abc import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="privata",
        description="Check a Python project for module privacy issues.",
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=Path.cwd(),
        metavar="project-root",
        type=Path,
        help="Project root to scan. Defaults to the current directory.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Privata module privacy checker."""
    args = _build_parser().parse_args(argv)
    return check_project(args.project_root)


if __name__ == "__main__":
    raise SystemExit(main())
