"""Fail when the documented pre-commit ``rev`` falls behind the latest release.

The install instructions pin a tag. Nothing moves that pin when a release goes
out, so it rots silently: it sat at v0.1.2 through five releases until someone
read the README. This check makes the rot visible at commit time.

A pin ahead of the latest tag is allowed, because a release-prep commit bumps
the docs before the tag exists.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

_PIN = re.compile(r"^\s*rev:\s*v(?P<version>\d+\.\d+\.\d+)\s*$", re.MULTILINE)
_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")


def _documented_pins(root: Path) -> dict[Path, set[tuple[int, ...]]]:
    pins: dict[Path, set[tuple[int, ...]]] = {}
    sources = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    for path in sources:
        if not path.is_file():
            continue
        found = {
            tuple(int(part) for part in match["version"].split("."))
            for match in _PIN.finditer(path.read_text(encoding="utf-8"))
        }
        if found:
            pins[path.relative_to(root)] = found
    return pins


def _latest_tag(root: Path) -> tuple[int, ...] | None:
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run(  # noqa: S603
        [git, "tag", "--list", "v*"],
        capture_output=True,
        check=False,
        cwd=root,
        text=True,
    )
    versions = [
        tuple(int(part) for part in match["version"].split("."))
        for line in result.stdout.splitlines()
        if (match := _TAG.match(line.strip()))
    ]
    return max(versions) if versions else None


def _format(version: tuple[int, ...]) -> str:
    return "v" + ".".join(str(part) for part in version)


def main() -> int:
    """Compare every documented pin against the latest release tag."""
    root = Path(__file__).resolve().parent.parent
    pins = _documented_pins(root)
    if not pins:
        print("No documented pre-commit rev found; nothing to check.")
        return 0

    problems: list[str] = []

    distinct = {version for versions in pins.values() for version in versions}
    if len(distinct) > 1:
        listed = ", ".join(_format(version) for version in sorted(distinct))
        problems.append(f"documented pins disagree ({listed}); they must all move together")

    latest = _latest_tag(root)
    if latest is None:
        print("No release tags available; skipping the freshness comparison.")
    else:
        for path, versions in sorted(pins.items()):
            stale = sorted(version for version in versions if version < latest)
            for version in stale:
                problems.append(
                    f"{path}: pins {_format(version)} but {_format(latest)} is released",
                )

    for problem in problems:
        print(problem)
    if problems:
        print("\nUpdate the rev in the install instructions, then re-run.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
