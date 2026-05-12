"""Shared helpers for talking to observability platforms (OTel, Langfuse,
Phoenix, …). Underscore-prefixed because consumers are the platform-specific
TraceStore / TraceEnricher adapters — not user code.

Each helper module owns one SDK and its lifecycle. Adapters that target the
same backend share a single client instance via the helper's
`get_or_create_*` registry so a single run with N OTel-shaped sinks
exports through one `TracerProvider`.
"""

from __future__ import annotations
