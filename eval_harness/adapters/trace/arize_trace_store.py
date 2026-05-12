"""Arize TraceStore — thin composition over `OtelTraceStore`.

Arize is OTel-native: the only thing this store changes from the upstream
OTel adapter is the endpoint (Arize's OTLP collector, default
``https://otlp.arize.com/v1``), the headers (`space_id` + `api_key`),
and the resource attributes (`model_id`, `model_version`,
`arize.space_id`, `deployment.environment`). Span emission logic comes
from `OtelTraceStore` unchanged — the "no duplicate OTel export logic"
invariant from ev-cjr / ev-hg6.

Sharing-with-OTel: two adapters (`OtelTraceStore` + `ArizeTraceStore`)
configured for the same endpoint + headers + resource attrs hit the same
fingerprint in `eval_harness._platforms.otel`'s registry and share a
`TracerProvider`.
"""

from __future__ import annotations

from typing import Any

from eval_harness._platforms.arize import (
    arize_otel_headers,
    arize_resource_attributes,
)
from eval_harness._platforms.otel import OtelClient
from eval_harness.adapters.trace.otel_trace_store import OtelTraceStore

_DEFAULT_OTLP_ENDPOINT = "https://otlp.arize.com/v1"


class ArizeTraceStore(OtelTraceStore):
    """Configures `OtelTraceStore` for an Arize collector. All span
    emission, write-only semantics, and shared-`TracerProvider` behaviour
    live in the parent class; this class only fixes endpoint, auth headers,
    and resource attributes."""

    def __init__(
        self,
        *,
        space_id: str | None = None,
        api_key: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        environment: str | None = None,
        endpoint: str = _DEFAULT_OTLP_ENDPOINT,
        headers: dict[str, str] | None = None,
        resource_attributes: dict[str, str] | None = None,
        protocol: str = "http",
        _client: OtelClient | None = None,
        **kwargs: Any,
    ) -> None:
        merged_headers = arize_otel_headers(
            space_id=space_id, api_key=api_key, extra=headers
        )
        attrs = arize_resource_attributes(
            model_id=model_id,
            model_version=model_version,
            space_id=space_id,
            environment=environment,
            extra=resource_attributes,
        )
        super().__init__(
            endpoint=endpoint,
            headers=merged_headers,
            protocol=protocol,
            resource_attributes=attrs,
            _client=_client,
            **kwargs,
        )
        self.space_id = space_id
        self.api_key = api_key
        self.model_id = model_id
        self.model_version = model_version
        self.environment = environment
