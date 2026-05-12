from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace, TraceOutput
from eval_harness.core.time import utc_now
from eval_harness.evaluators._embedders import embedder_registry
from eval_harness.evaluators.semantic_similarity import SemanticSimilarityEvaluator


class _FakeEmbedder:
    """Stub EmbedderBackend: returns vectors driven by a `mapping` dict.

    Any string not in the mapping gets a unique orthogonal-ish vector based on
    its hash, so unrelated strings stay near zero cosine similarity. Tests
    pre-seed `mapping[text] = vector` to force the relationships they need.
    """

    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        self.mapping = dict(mapping or {})
        self.calls: list[str] = []
        self.raise_on_call: Exception | None = None

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if text in self.mapping:
            return self.mapping[text]
        # Deterministic but text-distinct fallback.
        h = abs(hash(text)) % 1000 + 1
        return [float(h), 0.0, 0.0]


@pytest.fixture
def fake_embedder() -> Iterator[_FakeEmbedder]:
    """Registers a stub embedder under `fake`; restores prior state."""
    embedder = _FakeEmbedder()
    prior_factories = dict(embedder_registry._factories)
    prior_instances = dict(embedder_registry._instances)
    embedder_registry.register("fake", lambda: embedder)
    try:
        yield embedder
    finally:
        embedder_registry._factories = prior_factories
        embedder_registry._instances = prior_instances


def _trace(final_answer: str = "the city is Richmond") -> Trace:
    now = utc_now()
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=now,
        finished_at=now,
        latency_ms=1,
        input={},
        output=TraceOutput(final_answer=final_answer),
    )


# ---- validate_config -----------------------------------------------------


def test_validate_requires_embedder_name() -> None:
    with pytest.raises(ConfigError, match="embedder_name"):
        SemanticSimilarityEvaluator.validate_config({"reference_text": "x"})


def test_validate_requires_reference() -> None:
    with pytest.raises(ConfigError, match="reference"):
        SemanticSimilarityEvaluator.validate_config({"embedder_name": "fake"})


def test_validate_threshold_must_be_in_range() -> None:
    with pytest.raises(ConfigError, match="threshold"):
        SemanticSimilarityEvaluator.validate_config(
            {"embedder_name": "fake", "reference_text": "x", "threshold": 2.0}
        )


def test_validate_field_must_be_string() -> None:
    with pytest.raises(ConfigError, match="field"):
        SemanticSimilarityEvaluator.validate_config(
            {"embedder_name": "fake", "reference_text": "x", "field": 42}
        )


# ---- registry seam -------------------------------------------------------


def test_unknown_embedder_raises_with_install_hint() -> None:
    """Unknown embedder -> ConfigError that lists known names or points at
    the extras if none are installed."""
    with pytest.raises(ConfigError) as exc:
        SemanticSimilarityEvaluator(
            name="sim", embedder_name="never-registered", reference_text="x"
        )
    msg = str(exc.value)
    assert "never-registered" in msg
    # Either known backends are listed, or the install hint is present.
    has_known = "openai-text-embedding-3-small" in msg or "sentence-transformers" in msg
    has_hint = "eval-harness[openai]" in msg or "eval-harness[embeddings_local]" in msg
    assert has_known or has_hint


def test_unknown_embedder_with_empty_registry_emits_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no embedders are registered at all, the error MUST direct the
    user to the extras."""
    from eval_harness.evaluators._embedders import EmbedderRegistry

    empty = EmbedderRegistry()
    with pytest.raises(ConfigError) as exc:
        empty.resolve("anything")
    msg = str(exc.value)
    assert "eval-harness[openai]" in msg or "eval-harness[embeddings_local]" in msg


def test_evaluator_factory_registers_semantic_similarity() -> None:
    from eval_harness.factories import evaluator_factory

    assert "semantic_similarity" in evaluator_factory.registry.names()


def test_embedder_registry_is_singleton_per_name(fake_embedder: _FakeEmbedder) -> None:
    a = embedder_registry.resolve("fake")
    b = embedder_registry.resolve("fake")
    assert a is b


# ---- pass cases ----------------------------------------------------------


async def test_pass_when_similarity_above_threshold(fake_embedder: _FakeEmbedder) -> None:
    # Identical vectors -> cosine 1.0.
    fake_embedder.mapping = {
        "the answer": [1.0, 0.0],
        "the reference": [1.0, 0.0],
    }
    ev = SemanticSimilarityEvaluator(
        name="sim",
        embedder_name="fake",
        reference_text="the reference",
        threshold=0.8,
    )
    trace = _trace(final_answer="the answer")
    result = await ev.evaluate(EvalCase(id="c1", input={}), trace, None)
    assert result.passed is True
    assert math.isclose(result.score or 0.0, 1.0, rel_tol=1e-9)
    assert result.detail["embedder_name"] == "fake"
    assert result.detail["threshold"] == 0.8


async def test_reference_path_resolves_from_case(fake_embedder: _FakeEmbedder) -> None:
    """When reference_text isn't given, pull the reference from a JSONPath
    on the case (typically `case.expected.facts.*`)."""
    fake_embedder.mapping = {
        "an answer": [1.0, 0.0],
        "Richmond": [1.0, 0.0],
    }
    ev = SemanticSimilarityEvaluator(
        name="sim",
        embedder_name="fake",
        reference_path="$.case.expected.facts.suburb",
    )
    case = EvalCase(
        id="c1", input={}, expected=ExpectedBehavior(facts={"suburb": "Richmond"})
    )
    trace = _trace(final_answer="an answer")
    result = await ev.evaluate(case, trace, None)
    assert result.passed is True
    assert result.detail["reference_text"] == "Richmond"


# ---- fail cases ----------------------------------------------------------


async def test_fail_when_similarity_below_threshold(fake_embedder: _FakeEmbedder) -> None:
    # Orthogonal vectors -> cosine 0.0.
    fake_embedder.mapping = {
        "the answer": [1.0, 0.0],
        "the reference": [0.0, 1.0],
    }
    ev = SemanticSimilarityEvaluator(
        name="sim",
        embedder_name="fake",
        reference_text="the reference",
        threshold=0.8,
    )
    trace = _trace(final_answer="the answer")
    result = await ev.evaluate(EvalCase(id="c1", input={}), trace, None)
    assert result.passed is False
    assert math.isclose(result.score or 0.0, 0.0, abs_tol=1e-9)
    assert "0.0000 < threshold=0.8000" in result.reason


async def test_fail_when_answer_field_missing(fake_embedder: _FakeEmbedder) -> None:
    """Missing answer field -> embed empty string. With an orthogonal
    reference, cosine is well below threshold and the evaluator fails
    cleanly (no crash, no error)."""
    fake_embedder.mapping = {
        "": [1.0, 0.0],
        "the reference": [0.0, 1.0],
    }
    ev = SemanticSimilarityEvaluator(
        name="sim",
        embedder_name="fake",
        reference_text="the reference",
        field="output.nonexistent_field",
        threshold=0.99,
    )
    trace = _trace()
    result = await ev.evaluate(EvalCase(id="c1", input={}), trace, None)
    assert result.passed is False
    assert result.error is None  # missing field is a soft fail, not an error


# ---- error cases ---------------------------------------------------------


async def test_error_when_embedder_raises(fake_embedder: _FakeEmbedder) -> None:
    fake_embedder.raise_on_call = RuntimeError("rate limit")
    ev = SemanticSimilarityEvaluator(
        name="sim", embedder_name="fake", reference_text="x"
    )
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "embedder_error"
    assert "rate limit" in (result.error.message or "")


async def test_error_when_reference_path_resolves_to_nothing(
    fake_embedder: _FakeEmbedder,
) -> None:
    ev = SemanticSimilarityEvaluator(
        name="sim",
        embedder_name="fake",
        reference_path="$.case.expected.facts.absent",
    )
    case = EvalCase(id="c1", input={}, expected=ExpectedBehavior(facts={}))
    result = await ev.evaluate(case, _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "missing_reference"


async def test_error_when_embedding_dimensions_mismatch(
    fake_embedder: _FakeEmbedder,
) -> None:
    fake_embedder.mapping = {
        "the answer": [1.0, 0.0],
        "the reference": [1.0, 0.0, 0.0],
    }
    ev = SemanticSimilarityEvaluator(
        name="sim", embedder_name="fake", reference_text="the reference"
    )
    trace = _trace(final_answer="the answer")
    result = await ev.evaluate(EvalCase(id="c1", input={}), trace, None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "embedder_error"
    assert "dimension mismatch" in (result.error.message or "")


# ---- backend modules don't import heavy SDKs at module-load time ---------


def test_openai_backend_module_import_does_not_require_sdk() -> None:
    """Module import must work without `openai` installed; only constructing
    OpenAIEmbedder triggers the SDK check."""
    import importlib

    mod = importlib.import_module("eval_harness.evaluators._embedders.openai")
    assert hasattr(mod, "OpenAIEmbedder")


def test_sentence_transformers_backend_module_import_does_not_require_sdk() -> None:
    import importlib

    mod = importlib.import_module(
        "eval_harness.evaluators._embedders.sentence_transformers"
    )
    assert hasattr(mod, "SentenceTransformersEmbedder")


def test_openai_backend_raises_configerror_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "openai":
            raise ImportError("simulated missing openai")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = importlib.reload(
        importlib.import_module("eval_harness.evaluators._embedders.openai")
    )
    with pytest.raises(ConfigError) as exc:
        mod.OpenAIEmbedder()
    assert "openai" in str(exc.value).lower()
    assert "eval-harness[openai]" in str(exc.value)


def test_sentence_transformers_backend_raises_configerror_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "sentence_transformers":
            raise ImportError("simulated missing sentence_transformers")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = importlib.reload(
        importlib.import_module(
            "eval_harness.evaluators._embedders.sentence_transformers"
        )
    )
    with pytest.raises(ConfigError) as exc:
        mod.SentenceTransformersEmbedder()
    assert "sentence-transformers" in str(exc.value)
    assert "eval-harness[embeddings_local]" in str(exc.value)
