from __future__ import annotations

from typing import Any

import pytest

from eval_harness.core.config import EvaluatorConfig
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import RunVariant
from eval_harness.evaluators.base import Evaluator
from eval_harness.factories import (
    DatasetAdapterFactory,
    EvaluatorFactory,
    SystemAdapterFactory,
    TraceStoreFactory,
    WorkspaceFactory,
    dataset_adapter_factory,
    evaluator_factory,
    system_adapter_factory,
    trace_store_factory,
    workspace_factory,
)


def test_system_factory_unknown_type_raises_configerror() -> None:
    f = SystemAdapterFactory()
    f.register("known", object)
    with pytest.raises(ConfigError) as exc_info:
        f.build(RunVariant(name="v", adapter="missing", config={}))
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "known" in msg


def test_dataset_factory_unknown_type_raises_configerror() -> None:
    f = DatasetAdapterFactory()
    f.register("alpha", object)
    with pytest.raises(ConfigError) as exc_info:
        f.build({"type": "missing"})
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "alpha" in msg


def test_dataset_factory_missing_type_raises_configerror() -> None:
    f = DatasetAdapterFactory()
    with pytest.raises(ConfigError) as exc_info:
        f.build({})
    assert "type" in str(exc_info.value)


def test_trace_store_factory_unknown_type_raises_configerror() -> None:
    f = TraceStoreFactory()
    f.register("alpha", object)
    with pytest.raises(ConfigError) as exc_info:
        f.build({"type": "missing"})
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "alpha" in msg


def test_workspace_factory_unknown_type_raises_configerror() -> None:
    f = WorkspaceFactory()
    f.register("alpha", object)
    with pytest.raises(ConfigError) as exc_info:
        f.build({"type": "missing"})
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "alpha" in msg


def test_evaluator_factory_unknown_type_raises_configerror_listing_registered() -> None:
    f = EvaluatorFactory()
    f.register("alpha", object)
    f.register("beta", object)
    with pytest.raises(ConfigError) as exc_info:
        f.build(EvaluatorConfig(type="unknown", name="x", config={}))
    msg = str(exc_info.value)
    assert "unknown" in msg
    assert "alpha" in msg
    assert "beta" in msg


def test_evaluator_factory_calls_validate_config() -> None:
    seen: dict[str, Any] = {}

    class FakeEvaluator(Evaluator):
        type = "fake"

        @classmethod
        def validate_config(cls, config: dict[str, Any]) -> None:
            seen["called_with"] = config

        async def evaluate(self, case, trace, artifact):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    f = EvaluatorFactory()
    f.register("fake", FakeEvaluator)
    instance = f.build(EvaluatorConfig(type="fake", name="my_name", config={"k": "v"}))
    assert isinstance(instance, FakeEvaluator)
    assert instance.name == "my_name"
    assert seen["called_with"] == {"k": "v"}


def test_system_factory_builds_registered_adapter() -> None:
    f = SystemAdapterFactory()

    class FakeAdapter:
        def __init__(self, name: str, **config: Any) -> None:
            self.name = name
            self.config = config

    f.register("fake", FakeAdapter)
    inst = f.build(RunVariant(name="v1", adapter="fake", config={"k": 1}))
    assert isinstance(inst, FakeAdapter)
    assert inst.name == "v1"
    assert inst.config == {"k": 1}


def test_load_entry_points_idempotent_does_not_raise() -> None:
    f = SystemAdapterFactory()
    f.load_entry_points()
    f.load_entry_points()


def test_module_singletons_load_built_in_entry_points() -> None:
    # Importing the singletons module already triggered load_entry_points()
    # through each adapter subpackage's __init__.py.
    sys_names = system_adapter_factory.registry.names()
    assert "http" in sys_names
    assert "python_function" in sys_names

    eval_names = evaluator_factory.registry.names()
    assert "contains_text" in eval_names
    assert "tool_called" in eval_names
    assert "llm_judge" in eval_names
    assert "exact_match" in eval_names

    assert "yaml" in dataset_adapter_factory.registry.names()
    assert "local_files" in trace_store_factory.registry.names()
    assert "tempdir_snapshot" in workspace_factory.registry.names()
