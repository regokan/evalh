"""Semantic-similarity evaluator — cosine similarity between an answer and
a reference, both run through a pluggable embedder backend.

NO default embedder is shipped. Install one of:
    pip install 'eval-harness[openai]'              # API-backed
    pip install 'eval-harness[embeddings_local]'    # ~80MB local model

Then reference the embedder by name in the evaluator config:
    embedder_name: openai-text-embedding-3-small
    embedder_name: sentence-transformers/all-MiniLM-L6-v2
"""

from __future__ import annotations

import math
import traceback
from typing import Any, ClassVar

from jsonpath_ng import parse as jsonpath_parse

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
    TraceError,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators._embedders import EmbedderBackend, embedder_registry
from eval_harness.evaluators.base import Evaluator

_DEFAULT_FIELD = "output.final_answer"
_DEFAULT_THRESHOLD = 0.8


class SemanticSimilarityEvaluator(Evaluator):
    type: ClassVar[str] = "semantic_similarity"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        name = config.get("embedder_name")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                "semantic_similarity: 'embedder_name' (string) is required"
            )
        ref = config.get("reference_text")
        ref_path = config.get("reference_path")
        if ref is None and ref_path is None:
            raise ConfigError(
                "semantic_similarity: one of 'reference_text' or "
                "'reference_path' is required"
            )
        if ref is not None and not isinstance(ref, str):
            raise ConfigError("semantic_similarity: 'reference_text' must be a string")
        if ref_path is not None and not isinstance(ref_path, str):
            raise ConfigError(
                "semantic_similarity: 'reference_path' must be a JSONPath string"
            )
        threshold = config.get("threshold", _DEFAULT_THRESHOLD)
        if (
            not isinstance(threshold, int | float)
            or isinstance(threshold, bool)
            or not -1.0 <= float(threshold) <= 1.0
        ):
            raise ConfigError(
                "semantic_similarity: 'threshold' must be a number in [-1, 1]"
            )
        field = config.get("field", _DEFAULT_FIELD)
        if not isinstance(field, str):
            raise ConfigError("semantic_similarity: 'field' must be a string JSONPath")

    def __init__(self, name: str, **config: Any) -> None:
        super().__init__(name, **config)
        self._embedder_name: str = str(config["embedder_name"])
        self._reference_text: str | None = config.get("reference_text")
        self._reference_path: str | None = config.get("reference_path")
        self._threshold: float = float(config.get("threshold", _DEFAULT_THRESHOLD))
        self._field: str = str(config.get("field", _DEFAULT_FIELD))
        # Resolve at plan time so a missing extra fails before any case runs.
        self._embedder: EmbedderBackend = embedder_registry.resolve(self._embedder_name)

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()

        answer = _jsonpath_first(trace.model_dump(mode="python"), self._field)
        answer_text = "" if answer is None else str(answer)

        try:
            reference_text = self._resolve_reference(case)
        except ConfigError as e:
            return _error_result(
                self, case, trace, started, "missing_reference", str(e)
            )

        try:
            answer_vec, reference_vec = await self._embed_pair(answer_text, reference_text)
        except Exception as e:
            return _error_result(
                self,
                case,
                trace,
                started,
                "embedder_error",
                f"embedder call failed: {type(e).__name__}: {e}",
                stack=traceback.format_exc(),
            )

        try:
            similarity = _cosine(answer_vec, reference_vec)
        except ValueError as e:
            return _error_result(
                self, case, trace, started, "embedder_error", str(e)
            )

        passed = similarity >= self._threshold
        reason = (
            f"similarity={similarity:.4f} >= threshold={self._threshold:.4f}"
            if passed
            else f"similarity={similarity:.4f} < threshold={self._threshold:.4f}"
        )

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            score=similarity,
            reason=reason,
            detail={
                "embedder_name": self._embedder_name,
                "field": self._field,
                "similarity": similarity,
                "threshold": self._threshold,
                "reference_text": reference_text,
            },
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )

    def _resolve_reference(self, case: EvalCase) -> str:
        if self._reference_text is not None:
            return self._reference_text
        assert self._reference_path is not None
        payload = {"case": case.model_dump(mode="python")}
        value = _jsonpath_first(payload, self._reference_path)
        if value is None:
            raise ConfigError(
                f"reference_path {self._reference_path!r} resolved to no value "
                f"in case {case.id!r}"
            )
        return str(value)

    async def _embed_pair(
        self, answer: str, reference: str
    ) -> tuple[list[float], list[float]]:
        return (
            await self._embedder.embed(answer),
            await self._embedder.embed(reference),
        )


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(
            f"embedding dimension mismatch: len(answer)={len(a)} len(reference)={len(b)}"
        )
    if not a:
        raise ValueError("embedding is empty")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("embedding has zero norm; cosine undefined")
    return dot / (norm_a * norm_b)


def _jsonpath_first(data: dict[str, Any], path: str) -> Any:
    try:
        matches = jsonpath_parse(path).find(data)
    except Exception:
        return None
    return matches[0].value if matches else None


def _error_result(
    evaluator: SemanticSimilarityEvaluator,
    case: EvalCase,
    trace: Trace,
    started: Any,
    error_type: str,
    message: str,
    *,
    stack: str | None = None,
) -> EvaluationResult:
    finished = utc_now()
    return EvaluationResult(
        run_id=trace.run_id,
        case_id=case.id,
        variant_name=trace.variant_name,
        evaluator=evaluator.name,
        evaluator_type=evaluator.type,
        passed=False,
        reason=message,
        detail={"embedder_name": evaluator._embedder_name},
        started_at=started,
        finished_at=finished,
        latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        error=TraceError(type=error_type, message=message, stack=stack),
    )
