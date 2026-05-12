# tests/

Three layers, three jobs. Don't mix them. See `.claude/rules/testing.md` for the source of truth.

| Layer | Where | Network | API key | What it covers |
|---|---|---|---|---|
| **Unit** | `tests/unit/` | No (mocks) | No | One module / one class in isolation |
| **Integration** | `tests/integration/` | No (`respx` / `AsyncMock`) | No | Multiple components, mocked transports |
| **Smoke** | `examples/tiny_demo/` | Yes (Anthropic) | Yes (`ANTHROPIC_API_KEY`) | Real-LLM end-to-end |

`pyproject.toml` sets `asyncio_mode = "auto"` — `async def` test functions run directly, no decorator needed.

## Running

```bash
poetry run pytest tests/                  # unit + integration (offline; no network, no API key)
poetry run pytest tests/unit/             # unit only
poetry run pytest tests/integration/      # integration only
```

CI runs unit + integration. The smoke test is documented below; it costs money and needs a real API key, so it stays out of `pytest tests/`.

## Manual smoke

The smoke test confirms an actual Anthropic-backed run succeeds end-to-end and produces the durable on-disk surface described in `docs/DataModel.md > On-disk layout`.

```bash
export ANTHROPIC_API_KEY=sk-...
poetry run evalh run examples/tiny_demo/eval.yaml
```

Expect:

- Wall time under 60 seconds on a normal connection.
- A new directory at `runs/<ISO8601>_tiny_demo/` containing:
  - `config.yaml` — resolved, env-expanded, secrets masked
  - `config_hash.txt`
  - `traces.jsonl` — one `Trace` per line (3 cases × 2 variants = 6)
  - `results.jsonl` — one `EvaluationResult` per line
  - `summary.yaml` — `RunSummary` including a `ComparisonReport` against `agent_concise`

The smoke run is stochastic — assert shape, not text. The pass-rate per variant will vary across runs; that's the point.

## Fixtures

`tests/conftest.py` provides:

- `fake_judge_backend` — registers a deterministic fake into `eval_harness.judge_backends` (registered prefixes: `claude`, `gpt`). Tests that exercise `llm_judge` request this fixture instead of patching SDK internals. The registry is restored after the test.
- `deterministic_rng` (autouse) — seeds `random` with `42` and restores the prior state on teardown.

Per-component minimums (from `.claude/rules/testing.md`):
- Each evaluator: pass case + fail case + error case.
- Each adapter: open/run/close lifecycle + error → `Trace.error` + latency recorded.
- Each factory: unknown type → `ConfigError`; known type → instantiates.
- Runner: cells parallel + one-cell failure isolated + evaluator failure isolated + concurrency bounded.
