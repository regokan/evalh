"""Webhook reporting TraceStore.

Implements the TraceStore Protocol so it slots into `output:[...]` like
any other sink, but only `save_summary` is meaningful — the per-cell
hooks (save_trace / save_evaluation / save_artifact) are no-ops because
webhooks are inherently summary-grained.

Three platforms today:
  - `slack`   — Block Kit JSON via httpx (no SDK).
  - `discord` — embed JSON via httpx (no SDK).
  - `linear`  — Linear's GraphQL API via the official `linear-api` SDK,
                gated behind the `[webhook]` extra (Linear is the only
                platform that needs an SDK for createComment).

When listed as a non-first sink in `output:`, the runner's
multi-sink failure-soft logic (ev-7aj) records this store's exceptions
into `RunSummary.sink_errors` rather than aborting the run. The store
itself just raises cleanly on failure — symmetry with the rest of the
trace-store fleet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from eval_harness._platforms.webhook import (
    SummaryMessage,
    build_summary_message,
    format_discord,
    format_linear,
    format_slack,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)
from eval_harness.core.url import validate_url_scheme

_SUPPORTED_PLATFORMS = frozenset({"slack", "discord", "linear"})
_DEFAULT_TIMEOUT_SECONDS = 10.0


class WebhookTraceStore:
    """Posts a per-run summary to Slack / Discord / Linear."""

    def __init__(
        self,
        *,
        platform: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        team_id: str | None = None,
        issue_id: str | None = None,
        format: dict[str, Any] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        # Test seam: respx-mocked client or pre-built SDK shim.
        _http_client: httpx.AsyncClient | None = None,
        _linear_client: Any | None = None,
        **_extra: Any,
    ) -> None:
        if not platform:
            raise ConfigError(
                "webhook trace store: 'platform' is required "
                f"(one of {sorted(_SUPPORTED_PLATFORMS)})"
            )
        if platform not in _SUPPORTED_PLATFORMS:
            raise ConfigError(
                f"webhook trace store: unsupported platform {platform!r}; "
                f"choose from {sorted(_SUPPORTED_PLATFORMS)}"
            )
        if platform != "linear" and not url:
            raise ConfigError(
                f"webhook trace store ({platform}): 'url' is required "
                "(the webhook URL)"
            )
        if platform != "linear" and url is not None:
            # Reject `file://`, `gopher://`, plain `http://` to non-localhost,
            # etc. Per `.claude/rules/security.md`. Shared helper keeps the
            # rule in lockstep with the http SystemAdapter.
            validate_url_scheme(
                url, adapter_name=f"webhook trace store ({platform})"
            )
        if platform == "linear" and _linear_client is None and not api_key:
            raise ConfigError(
                "webhook trace store (linear): 'api_key' is required "
                "(Linear personal/API key); install via "
                "`pip install 'eval-harness[webhook]'`"
            )
        self.platform = platform
        self.url = url
        self.api_key = api_key
        self.team_id = team_id
        self.issue_id = issue_id
        self.format_options = dict(format or {})
        self.timeout_seconds = float(timeout_seconds)

        self._owns_http = _http_client is None
        self._http_client: httpx.AsyncClient = _http_client or httpx.AsyncClient(
            timeout=self.timeout_seconds
        )
        self._linear_client = _linear_client
        self._run_id: str = ""
        self.rendered_config: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http:
            await self._http_client.aclose()

    async def open(self, run_id: str, run_dir: Path) -> None:
        self._run_id = run_id

    async def close(self) -> None:
        return None

    # ---- save_summary: the only meaningful hook -------------------------

    async def save_summary(self, summary: RunSummary) -> None:
        message = build_summary_message(summary)
        if self.platform == "slack":
            await self._post_slack(message)
        elif self.platform == "discord":
            await self._post_discord(message)
        elif self.platform == "linear":
            await self._post_linear(message)

    # ---- no-ops (webhook reporting is summary-grained) -----------------

    async def save_trace(self, trace: Trace) -> None:
        return None

    async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
        # Webhook reporting is summary-grained — no per-trace work to dedupe.
        return True

    async def save_evaluation(
        self, case_id: str, variant: str, results: list[EvaluationResult]
    ) -> None:
        return None

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        return None

    # ---- read methods (write-only sink) --------------------------------

    def iter_traces(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[Trace]:
        return _empty_iter()

    def iter_results(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[EvaluationResult]:
        return _empty_iter()

    async def load_summary(self, run_id: str) -> RunSummary | None:
        return None

    async def list_run_ids(self) -> list[str]:
        return []

    # ---- per-platform POSTs --------------------------------------------

    async def _post_slack(self, message: SummaryMessage) -> None:
        payload = format_slack(message)
        resp = await self._http_client.post(self.url or "", json=payload)
        # Slack incoming-webhook returns 200 with body "ok" on success.
        if resp.status_code >= 300:
            raise AdapterError(
                f"webhook trace store (slack): HTTP {resp.status_code} from "
                f"{self.url}: {resp.text[:200]}"
            )

    async def _post_discord(self, message: SummaryMessage) -> None:
        payload = format_discord(message)
        resp = await self._http_client.post(self.url or "", json=payload)
        # Discord returns 204 No Content on success.
        if resp.status_code >= 300:
            raise AdapterError(
                f"webhook trace store (discord): HTTP {resp.status_code} from "
                f"{self.url}: {resp.text[:200]}"
            )

    async def _post_linear(self, message: SummaryMessage) -> None:
        body = format_linear(message)
        client = self._linear_client
        if client is None:
            client = _build_linear_client(self.api_key)
        try:
            create = getattr(client, "create_comment", None) or getattr(
                client, "createComment", None
            )
            if create is None:
                raise AdapterError(
                    "webhook trace store (linear): client has no "
                    "`create_comment` method"
                )
            kwargs: dict[str, Any] = {"body": body}
            if self.issue_id:
                kwargs["issue_id"] = self.issue_id
            if self.team_id:
                kwargs["team_id"] = self.team_id
            result = create(**kwargs)
            # Linear SDK calls may be sync; await only if needed.
            if hasattr(result, "__await__"):
                await result
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                f"webhook trace store (linear): "
                f"{type(e).__name__}: {e}"
            ) from e


def _build_linear_client(api_key: str | None) -> Any:
    """Lazy SDK import — raises ConfigError with install hint when the
    [webhook] extra isn't installed."""
    try:
        from linear_api import LinearClient
    except ImportError as e:
        raise ConfigError(
            "webhook trace store (linear): requires the linear-api SDK. "
            "Install with: pip install 'eval-harness[webhook]'"
        ) from e
    return LinearClient(api_key=api_key)


async def _empty_iter() -> AsyncIterator[Any]:
    if False:
        yield  # pragma: no cover — keep the async-generator shape
