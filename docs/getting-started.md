---
icon: lucide/rocket
---

# Getting Started

## Prerequisites

You need:

- Python 3.12+
- a Python project with source code under `src/`

## Installation

=== "uv tool"

    ```bash
    uv tool install privata
    ```

=== "pipx"

    ```bash
    pipx install privata
    ```

=== "pip"

    ```bash
    pip install privata
    ```

=== "From source"

    ```bash
    git clone https://github.com/basnijholt/privata.git
    cd privata
    uv sync --extra dev
    ```

## Run

From a project root:

```bash
privata .
```

Privata exits with status `0` when no privacy issues are found.
It exits with status `1` when it finds public symbols that can be made private or private module imports that cross package boundaries.

## Expected Layout

Privata expects a `src/` directory:

```text
project/
├── pyproject.toml
└── src/
    └── package/
        ├── __init__.py
        └── module.py
```

Tests can live anywhere.
Imports from tests do not count when deciding whether a symbol should stay public.
