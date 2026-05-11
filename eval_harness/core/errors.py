from __future__ import annotations


class ConfigError(Exception):
    """Raised at plan time, before any case runs."""


class AdapterError(Exception):
    """Raised at run time; the runner catches and turns this into ``Trace.error``."""


class RetriableError(AdapterError):
    """Subclass of AdapterError that the runner is allowed to retry."""
