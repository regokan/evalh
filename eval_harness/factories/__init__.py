from __future__ import annotations

from eval_harness.factories.dataset_adapter_factory import DatasetAdapterFactory
from eval_harness.factories.evaluator_factory import EvaluatorFactory
from eval_harness.factories.system_adapter_factory import SystemAdapterFactory
from eval_harness.factories.trace_enricher_factory import TraceEnricherFactory
from eval_harness.factories.trace_store_factory import TraceStoreFactory
from eval_harness.factories.workspace_factory import WorkspaceFactory

system_adapter_factory = SystemAdapterFactory()
dataset_adapter_factory = DatasetAdapterFactory()
trace_store_factory = TraceStoreFactory()
workspace_factory = WorkspaceFactory()
evaluator_factory = EvaluatorFactory()
trace_enricher_factory = TraceEnricherFactory()

__all__ = [
    "DatasetAdapterFactory",
    "EvaluatorFactory",
    "SystemAdapterFactory",
    "TraceEnricherFactory",
    "TraceStoreFactory",
    "WorkspaceFactory",
    "dataset_adapter_factory",
    "evaluator_factory",
    "system_adapter_factory",
    "trace_enricher_factory",
    "trace_store_factory",
    "workspace_factory",
]
