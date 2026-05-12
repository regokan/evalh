"""Phoenix TraceStore — thin composition over `OtelTraceStore`.

Phoenix is OTel-native: the only thing the TraceStore changes from the
upstream OTel adapter is the endpoint URL (`<base>/v1/traces`) and the
resource attributes (`openinference.project.name`). Span emission logic
comes from `OtelTraceStore` unchanged — the "no duplicate OTel export
logic" invariant from ev-cjr.

Sharing-with-OTel: because Phoenix's collector endpoint is just an OTLP
URL, two adapters (one `OtelTraceStore` + one `PhoenixTraceStore`)
pointed at the same Phoenix instance with the same project + headers
hit the same fingerprint in `eval_harness._platforms.otel`'s registry
and share a `TracerProvider`.
"""

from __future__ import annotations

from typing import Any

from eval_harness._platforms.otel import OtelClient
from eval_harness._platforms.phoenix import (
    phoenix_resource_attributes,
    phoenix_to_otel_endpoint,
)
from eval_harness.adapters.trace.otel_trace_store import OtelTraceStore


class PhoenixTraceStore(OtelTraceStore):
    """Configures `OtelTraceStore` for a Phoenix collector. All span
    emission, write-only semantics, and shared-`TracerProvider` behaviour
    live in the parent class; this class only fixes endpoint + resource
    attributes."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        project_name: str | None = None,
        headers: dict[str, str] | None = None,
        resource_attributes: dict[str, str] | None = None,
        protocol: str = "http",
        _client: OtelClient | None = None,
        **kwargs: Any,
    ) -> None:
        if not base_url:
            from eval_harness.core.errors import ConfigError

            raise ConfigError(
                "phoenix trace store: 'base_url' is required "
                "(e.g. 'http://phoenix:6006')"
            )
        merged_headers: dict[str, str] = dict(headers or {})
        if api_key and "Authorization" not in merged_headers:
            merged_headers["Authorization"] = f"Bearer {api_key}"
        attrs = phoenix_resource_attributes(
            project_name=project_name, extra=resource_attributes
        )
        super().__init__(
            endpoint=phoenix_to_otel_endpoint(base_url),
            headers=merged_headers,
            protocol=protocol,
            resource_attributes=attrs,
            _client=_client,
            **kwargs,
        )
        self.base_url = base_url
        self.api_key = api_key
        self.project_name = project_name
