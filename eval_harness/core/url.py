"""URL helpers shared by adapters that take user-supplied URLs.

Per `.claude/rules/security.md`: adapters that hit user-configured URLs
MUST validate the scheme at the config boundary — `https://` for
production, `http://localhost*` only for dev. Reject `file://`,
`gopher://`, etc. Used by `http_adapter` and `webhook_trace_store`;
new HTTP-shaped adapters should call this helper rather than
re-implementing the check inline (drift between callers is the failure
mode this module exists to prevent).
"""

from __future__ import annotations

from urllib.parse import urlparse

from eval_harness.core.errors import ConfigError


def validate_url_scheme(url: str, *, adapter_name: str) -> None:
    """Reject any URL whose scheme isn't `https://` or
    `http://localhost*`. Raises `ConfigError` with a message scoped by
    `adapter_name` so users see which adapter rejected the URL."""
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise ConfigError(
            f"{adapter_name}: invalid URL '{url}': {e}"
        ) from e
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return
    if scheme == "http":
        host = (parsed.hostname or "").lower()
        if host == "localhost" or host == "127.0.0.1" or host.startswith("127."):
            return
        raise ConfigError(
            f"{adapter_name}: plain http:// only allowed for localhost; got '{url}'"
        )
    raise ConfigError(
        f"{adapter_name}: unsupported URL scheme '{scheme}' in '{url}' "
        f"(only https:// and http://localhost are allowed)"
    )


__all__ = ["validate_url_scheme"]
