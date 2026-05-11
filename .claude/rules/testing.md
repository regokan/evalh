# Testing rules — deltas

Three layers, three jobs. Don't mix them.

| Layer | Where | Network | API key | What it covers |
|---|---|---|---|---|
| **Unit** | `tests/unit/` | No (mocks) | No | One module / one class in isolation |
| **Integration** | `tests/integration/` | No (`respx`) | No | Multiple components, mocked transports |
| **Smoke** | `examples/tiny_demo/` | Yes (Anthropic) | Yes | Real-LLM end-to-end |

`pyproject.toml` has `asyncio_mode = "auto"` — test functions can be `async def` directly, no decorator needed.

## Per-component minimums

- **Each evaluator**: pass case + fail case + error case (e.g., judge schema mismatch).
- **Each adapter**: open/run/close lifecycle + error → `Trace.error` + latency recorded.
- **Each factory**: unknown type → `ConfigError`; known type → instantiates.
- **Runner**: cells parallel + one-cell failure isolated + evaluator failure isolated + concurrency bounded.

## Mocking conventions

- HTTP → `respx.mock` with explicit route matchers. Don't mock the whole client.
- Anthropic SDK → patch `AsyncAnthropic.messages.create` with `unittest.mock.AsyncMock`. Don't patch the module-level `anthropic` import.
- LLM judge → inject a fake backend through the `eval_harness.judge_backends` registry. The registry is the seam.
- Filesystem → use pytest's `tmp_path`. Each test gets its own dir.

## Determinism

- Pin random seeds.
- Don't assert ordering of concurrent tasks unless the test serialized them.
- For timestamp comparisons: use `freezegun` or pass an explicit clock to the runner via test config.

## What NOT to test

- That `print()` outputs the right string. Use `caplog` or `rich`'s testing utilities.
- Third-party library behavior. Trust pydantic, httpx, anthropic-sdk.
- Stochastic example outputs. The smoke test asserts shape (run dir exists, schema is valid), not text.

## Naming

`test_<unit>_<scenario>_<expected>`:
- `test_runner_one_cell_timeout_records_error_in_trace`
- `test_http_adapter_response_mapping_extracts_final_answer`

Bad: `test_basic`, `test_it_works`, `test_1`.

## CI

`.github/workflows/ci.yml`: `ruff check`, `mypy`, `pytest tests/`. Smoke (`examples/tiny_demo/`) runs in a separate workflow gated on a label or main-branch push, since it costs money and needs `ANTHROPIC_API_KEY`.
