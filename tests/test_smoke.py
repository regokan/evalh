"""Placeholder smoke test.

Exists so `pytest` returns 0 against the empty package skeleton. The Mayor
will replace this with proper unit + integration tests per the layout in
RepositoryStructure.md.
"""

import eval_harness


def test_package_imports():
    assert eval_harness.__version__ == "0.0.1"
