"""End-to-end verification that the entry-point plugin story works.

Drives the v0.1 done-when criterion: an external package can add an
`Evaluator` to eval-harness with zero changes to eval-harness source.

The plugin package lives at `tests/fixtures/plugin_pkg/`. This test:
1. `pip install -e` it into a fresh venv (subprocess) so the parent test
   process's already-loaded entry-points don't mask the result.
2. Spawns another subprocess that imports `eval_harness` and queries its
   evaluator factory; asserts the plugin's `sql_equivalent` evaluator is
   registered and instantiable, and that it actually evaluates a trace
   against a seeded in-memory sqlite DB.

We use subprocesses because Python's `importlib.metadata` caches the
distribution list for the duration of an interpreter and editable installs
during the *current* run don't reliably refresh that cache.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import venv
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_PKG = _REPO_ROOT / "tests" / "fixtures" / "plugin_pkg"


@pytest.fixture(scope="module")
def installed_plugin_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a venv, install eval-harness + the plugin pkg into it. Returns the
    python executable inside the venv.

    Module-scoped so the slow `pip install` only runs once per test session.
    """
    venv_dir = tmp_path_factory.mktemp("plugin_venv")
    venv.EnvBuilder(with_pip=True, symlinks=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    if not python.exists():  # Windows
        python = venv_dir / "Scripts" / "python.exe"

    # Quiet pip and avoid network for eval-harness itself (already on disk).
    common = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    subprocess.run([*common, "-e", str(_REPO_ROOT)], check=True)
    subprocess.run([*common, "-e", str(_PLUGIN_PKG)], check=True)
    return python


def _run_in_venv(python: Path, script: str) -> dict[str, Any]:
    """Run a Python script inside the venv; expect a single JSON object on stdout."""
    result = subprocess.run(
        [str(python), "-c", textwrap.dedent(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return payload


def test_plugin_registered_via_entry_point(installed_plugin_venv: Path) -> None:
    """The plugin's evaluator appears in the evaluator-factory registry without
    eval-harness source being modified."""
    out = _run_in_venv(
        installed_plugin_venv,
        """
        import json
        # Importing the package triggers entry-point loading.
        import eval_harness.evaluators  # noqa: F401
        from eval_harness.factories import evaluator_factory
        names = evaluator_factory.registry.names()
        print(json.dumps({"names": names}))
        """,
    )
    assert "sql_equivalent" in out["names"]
    for builtin in ("contains_text", "tool_called", "llm_judge", "exact_match"):
        assert builtin in out["names"], f"builtin {builtin} missing"


def test_factory_builds_plugin_evaluator(installed_plugin_venv: Path) -> None:
    """`EvaluatorFactory.build` instantiates the plugin's class from a YAML-style
    config — the same path eval.yaml takes at run time."""
    out = _run_in_venv(
        installed_plugin_venv,
        """
        import json
        import eval_harness.evaluators  # noqa: F401
        from eval_harness.core.config import EvaluatorConfig
        from eval_harness.factories import evaluator_factory

        cfg = EvaluatorConfig(
            type="sql_equivalent",
            name="query_correctness",
            config={
                "reference_sql": "SELECT id FROM listings WHERE suburb='Richmond'",
                "setup_sql": [
                    "CREATE TABLE listings (id INTEGER, suburb TEXT);"
                    "INSERT INTO listings VALUES (1, 'Richmond'), (2, 'Carlton');"
                ],
            },
        )
        instance = evaluator_factory.build(cfg)
        print(json.dumps({
            "class_module": type(instance).__module__,
            "class_name": type(instance).__name__,
            "type_attr": instance.type,
            "name_attr": instance.name,
        }))
        """,
    )
    assert out["class_module"] == "plugin_pkg.sql_equivalent"
    assert out["class_name"] == "SqlEquivalentEvaluator"
    assert out["type_attr"] == "sql_equivalent"
    assert out["name_attr"] == "query_correctness"


def test_plugin_evaluator_actually_evaluates(installed_plugin_venv: Path) -> None:
    """Run the plugin's evaluate() on a synthetic trace and confirm it
    distinguishes a matching candidate SQL from a non-matching one."""
    out = _run_in_venv(
        installed_plugin_venv,
        """
        import asyncio, json
        from datetime import UTC, datetime
        import eval_harness.evaluators  # noqa: F401
        from eval_harness.core.config import EvaluatorConfig
        from eval_harness.core.models import EvalCase, Trace, TraceOutput
        from eval_harness.factories import evaluator_factory

        cfg = EvaluatorConfig(
            type="sql_equivalent",
            name="q",
            config={
                "reference_sql": "SELECT id FROM listings WHERE suburb='Richmond'",
                "setup_sql": [
                    "CREATE TABLE listings (id INTEGER, suburb TEXT);"
                    "INSERT INTO listings VALUES (1, 'Richmond'), (2, 'Carlton');"
                ],
            },
        )
        ev = evaluator_factory.build(cfg)

        now = datetime(2026, 5, 12, tzinfo=UTC)
        def trace(answer):
            return Trace(
                run_id="r1", case_id="c1", variant_name="v1",
                started_at=now, finished_at=now, latency_ms=0,
                input={}, output=TraceOutput(final_answer=answer),
            )

        case = EvalCase(id="c1", input={})

        async def main():
            ok = await ev.evaluate(case, trace("SELECT id FROM listings WHERE suburb='Richmond'"), None)
            bad = await ev.evaluate(case, trace("SELECT id FROM listings WHERE suburb='Carlton'"), None)
            return {"ok_passed": ok.passed, "bad_passed": bad.passed,
                    "ok_score": ok.score, "bad_score": bad.score}

        print(json.dumps(asyncio.run(main())))
        """,
    )
    assert out["ok_passed"] is True
    assert out["bad_passed"] is False
    assert out["ok_score"] == 1.0
    assert out["bad_score"] == 0.0
