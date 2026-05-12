from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from eval_harness.core.config import EvalConfig
from eval_harness.core.errors import ConfigError

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader that only treats true/false (any case) as booleans.

    Prevents YAML 1.1 from coercing `on`, `off`, `yes`, `no` to booleans —
    those appear as field names / values in our config (e.g., `retry.on`).
    """


# Copy SafeLoader's resolvers so we don't mutate the parent class, then strip
# the bool resolver and re-add a tighter one matching only true/false.
_StrictBoolLoader.yaml_implicit_resolvers = {
    ch: [r for r in resolvers if r[0] != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
# why-untyped-call: PyYAML's add_implicit_resolver is typed but its stub marks
# the classmethod as untyped; calling it is safe and unavoidable here.
_StrictBoolLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_config(path: Path) -> EvalConfig:
    """Load and validate an eval.yaml file.

    Pipeline: read YAML -> expand ${VAR} / ${VAR:-default} -> pydantic-validate
    -> cross-reference checks. Raises ConfigError on any failure.
    """
    try:
        raw = path.read_text()
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from e

    try:
        data = yaml.load(raw, Loader=_StrictBoolLoader)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {path}: {e}") from e

    if data is None:
        raise ConfigError(f"Config file {path} is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level of {path} must be a mapping, got {type(data).__name__}")

    expanded = _expand_env_vars(data, "")

    try:
        config = EvalConfig.model_validate(expanded)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e, path)) from e

    _cross_reference_checks(config)
    return config


def _expand_env_vars(value: Any, dotted_path: str) -> Any:
    if isinstance(value, str):
        return _expand_string(value, dotted_path)
    if isinstance(value, dict):
        return {
            k: _expand_env_vars(v, f"{dotted_path}.{k}" if dotted_path else str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand_env_vars(v, f"{dotted_path}[{i}]") for i, v in enumerate(value)]
    return value


def _expand_string(value: str, dotted_path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None:
            return env_val
        if default is not None:
            return default
        raise ConfigError(
            f"Missing environment variable '{var_name}' referenced at '{dotted_path}'"
        )

    return _ENV_VAR_RE.sub(replace, value)


def _format_validation_error(e: ValidationError, path: Path) -> str:
    lines = [f"Invalid config {path}:"]
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  at '{loc}': {err['msg']} ({err['type']})")
    return "\n".join(lines)


def _cross_reference_checks(config: EvalConfig) -> None:
    evaluator_names = {e.name for e in config.evaluators}
    for refs, label in (
        (config.pass_criteria.all_required, "all_required"),
        (config.pass_criteria.any_required, "any_required"),
    ):
        for name in refs:
            if name not in evaluator_names:
                raise ConfigError(
                    f"pass_criteria.{label} references unknown evaluator "
                    f"'{name}'; defined evaluators: {sorted(evaluator_names)}"
                )

    if config.run.baseline_variant is not None:
        system_names = {s.name for s in config.systems}
        if config.run.baseline_variant not in system_names:
            raise ConfigError(
                f"run.baseline_variant '{config.run.baseline_variant}' is not "
                f"in systems[]; defined systems: {sorted(system_names)}"
            )
