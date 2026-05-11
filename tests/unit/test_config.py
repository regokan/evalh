from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from eval_harness.core.config import EvalConfig, OutputConfig
from eval_harness.core.config_loader import load_config
from eval_harness.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, body: str, name: str = "eval.yaml") -> Path:
    p = tmp_path / name
    p.write_text(dedent(body))
    return p


def _minimal_yaml() -> str:
    return """
    eval:
      name: tiny
    dataset:
      type: yaml
      path: cases.yaml
    systems:
      - name: a
        adapter: python_function
    evaluators:
      - name: eval_one
        type: contains_text
        config:
          any_of: [hi]
    output:
      - type: local_files
        path: runs/
    """


def test_load_tiny_demo_example_validates() -> None:
    path = REPO_ROOT / "examples" / "tiny_demo" / "eval.yaml"
    cfg = load_config(path)
    assert isinstance(cfg, EvalConfig)
    assert cfg.eval.name == "tiny_demo"
    assert {s.name for s in cfg.systems} == {"agent_concise", "agent_verbose"}
    assert cfg.run.baseline_variant == "agent_concise"
    assert cfg.output[0].type == "local_files"


def test_load_listing_price_example_with_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_API_KEY", "secret-token")
    path = REPO_ROOT / "examples" / "listing_price" / "eval.yaml"
    cfg = load_config(path)
    assert cfg.eval.name == "listing_price_eval"
    headers = cfg.systems[0].model_dump().get("headers", {})
    assert headers["Authorization"] == "Bearer secret-token"


def test_top_level_typo_raises_configerror_naming_field(tmp_path: Path) -> None:
    # 'evluator' is a misspelling of 'evaluators' (a non-existent extra key)
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: cases.yaml
        systems:
          - name: a
            adapter: python_function
        evluator:
          - name: bogus
            type: contains_text
        evaluators:
          - name: e1
            type: contains_text
        output:
          - type: local_files
            path: runs/
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    assert "evluator" in str(exc_info.value)


def test_env_var_missing_raises_configerror(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: ${THIS_VAR_DOES_NOT_EXIST_12345}/cases.yaml
        systems:
          - name: a
            adapter: python_function
        evaluators:
          - name: e1
            type: contains_text
        output:
          - type: local_files
            path: runs/
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    msg = str(exc_info.value)
    assert "THIS_VAR_DOES_NOT_EXIST_12345" in msg
    assert "dataset.path" in msg


def test_env_var_default_resolves_when_unset(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: ${SOME_MISSING_VAR_XYZ:-fallback}/cases.yaml
        systems:
          - name: a
            adapter: python_function
        evaluators:
          - name: e1
            type: contains_text
        output:
          - type: local_files
            path: runs/
        """,
    )
    cfg = load_config(p)
    assert cfg.dataset.path == "fallback/cases.yaml"


def test_env_var_set_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_VAR", "actual")
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: ${MY_VAR:-fallback}/cases.yaml
        systems:
          - name: a
            adapter: python_function
        evaluators:
          - name: e1
            type: contains_text
        output:
          - type: local_files
            path: runs/
        """,
    )
    cfg = load_config(p)
    assert cfg.dataset.path == "actual/cases.yaml"


def test_pass_criteria_unknown_evaluator_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: cases.yaml
        systems:
          - name: a
            adapter: python_function
        evaluators:
          - name: real_eval
            type: contains_text
        pass_criteria:
          all_required: [does_not_exist]
        output:
          - type: local_files
            path: runs/
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    msg = str(exc_info.value)
    assert "does_not_exist" in msg
    assert "all_required" in msg


def test_baseline_variant_unknown_system_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: cases.yaml
        systems:
          - name: real_system
            adapter: python_function
        evaluators:
          - name: e1
            type: contains_text
        run:
          baseline_variant: ghost
        output:
          - type: local_files
            path: runs/
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    msg = str(exc_info.value)
    assert "ghost" in msg
    assert "baseline_variant" in msg


def test_output_single_mapping_coerced_to_list(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: cases.yaml
        systems:
          - name: a
            adapter: python_function
        evaluators:
          - name: e1
            type: contains_text
        output:
          type: local_files
          path: runs/
        """,
    )
    cfg = load_config(p)
    assert len(cfg.output) == 1
    assert isinstance(cfg.output[0], OutputConfig)
    assert cfg.output[0].type == "local_files"


def test_systems_allow_adapter_specific_fields(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        eval:
          name: x
        dataset:
          type: yaml
          path: cases.yaml
        systems:
          - name: a
            adapter: http
            endpoint: http://localhost:8000/chat
            timeout_seconds: 30
            headers:
              Authorization: Bearer xyz
        evaluators:
          - name: e1
            type: contains_text
        output:
          - type: local_files
            path: runs/
        """,
    )
    cfg = load_config(p)
    sys_dump = cfg.systems[0].model_dump()
    assert sys_dump["endpoint"] == "http://localhost:8000/chat"
    assert sys_dump["headers"] == {"Authorization": "Bearer xyz"}


def test_empty_file_raises_configerror(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ConfigError):
        load_config(p)


def test_malformed_yaml_raises_configerror(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("eval:\n  name: x\n   bad_indent: y\n")
    with pytest.raises(ConfigError) as exc_info:
        load_config(p)
    assert "YAML" in str(exc_info.value) or "parse" in str(exc_info.value).lower()


def test_minimal_valid_config(tmp_path: Path) -> None:
    p = _write(tmp_path, _minimal_yaml())
    cfg = load_config(p)
    assert cfg.schema_version == "1.0"
    assert cfg.run.max_concurrency == 4
    assert cfg.pass_criteria.all_required == []
