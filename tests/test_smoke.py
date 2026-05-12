"""Top-level smoke test.

Asserts the package imports and that ``eval_harness.__version__`` matches
``project.version`` in ``pyproject.toml``. Catches the
bump-pyproject-but-forget-__init__.py mismatch (or vice-versa) that the
PyPI publish step relies on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import eval_harness


def _pyproject_version() -> str:
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return str(data["project"]["version"])


def test_package_imports() -> None:
    assert eval_harness.__version__ == _pyproject_version()
