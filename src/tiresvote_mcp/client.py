"""HTTP client, configuration, retries, errors and secret redaction.

Wraps the public Tires API (``GET https://api.wheel-size.com/v2/tires/…``).
Configuration is explicit: a :class:`TiresSettings` instance is passed in, and
``TiresClient`` accepts an optional ``httpx`` transport for injection in tests.
``create_server()`` is the only place that builds settings from the process
environment — no module-level singleton reads env vars at import time.

Retry policy (bounded): at most ``max_retries`` retries (default 2, never more)
after the first attempt, only for HTTP 429/500/502/503/504 and transport-level
errors (connect/read timeouts included, since ``httpx.TimeoutException`` is a
``TransportError``). Other errors are never retried.

The API key is sent upstream as the ``user_key`` query parameter and must never
appear in MCP output: every payload leaving ``get()`` is scrubbed recursively,
``user_key`` params are stripped from embedded URLs, error messages are
sanitized (redaction before truncation), exception chains are suppressed, and a
logging filter keeps ``user_key`` out of httpx's request logs.

Retry/backoff skeleton adapted from wheel-size-mcp (MIT License,
Copyright (c) 2026 WHEEL SIZE KZ LLP) and adjusted to the Tires API contract:
DRF error bodies, plain-text auth/rate-limit responses, an injectable Host
header and an explicit endpoint allowlist.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections.abc import Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastmcp.exceptions import ToolError

API_PREFIX = "/v2/tires"
DEFAULT_BASE_URL = "https://api.wheel-size.com"
DEFAULT_TIMEOUT = 30.0

# Upstream pagination links may point back to the TiresVote HTML site once the
# API page limit is reached. Only these hosts are surfaced as site links.
SITE_HOSTS = frozenset({"tiresvote.com", "www.tiresvote.com"})

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 2  # retries after the first attempt => up to 3 requests total
BACKOFF_BASE = 0.5  # seconds, doubles per retry
RETRY_AFTER_CAP = 5.0  # seconds; bound on honoring the Retry-After header

# Approved public GET endpoints of the first version (docs/tools-inventory.md).
# Excluded upstream routes (/articles/, /top-charts/…) are absent on purpose:
# the allowlist — not a prefix match — decides what this client may call.
ALLOWED_PATHS = frozenset(
    {
        f"{API_PREFIX}/catalog/",
        f"{API_PREFIX}/catalog/{{brand}}/",
        f"{API_PREFIX}/catalog/{{brand}}/{{product}}/",
        f"{API_PREFIX}/catalog/{{brand}}/{{product}}/bnb/",
        f"{API_PREFIX}/catalog/{{brand}}/{{product}}/modes/",
        f"{API_PREFIX}/catalog/{{brand}}/{{product}}/materials/",
        f"{API_PREFIX}/search/",
        f"{API_PREFIX}/search/advanced/",
        f"{API_PREFIX}/regions/",
        f"{API_PREFIX}/performance-categories/",
        f"{API_PREFIX}/tests/",
        f"{API_PREFIX}/tests/{{slug}}/",
    }
)

# Resolved paths: slug segments only ([a-z0-9_-]), single slashes, trailing
# slash — rejects traversal, %-encoding, query/fragment injection and stray
# placeholders by construction.
_PATH_RESULT_RE = re.compile(r"^/v2/tires/[a-z0-9_-]+(?:/[a-z0-9_-]+)*/$")
_PATH_PARAM_RE = re.compile(r"^[-A-Za-z0-9_]+$")

_USER_KEY_RE = re.compile(r"\buser_key=[^\s&\"'<>]+", re.IGNORECASE)
_CRED_PARAM_RE = re.compile(
    r"\b(user_key|api_key|apikey|access_token|token|secret|password)=[^\s&\"'<>]+",
    re.IGNORECASE,
)

_BODY_SNIPPET = 300
_MAX_ERROR_LINES = 10

# Map upstream API parameter codes to the MCP-facing names users see, plus
# actionable hints for the most common invalid-value errors. rf is the
# production-verified 'runflat_filter' flag (docs/contract-decisions).
_API_TO_MCP_PARAM = {
    "b": "brands",
    "reg": "regions",
    "s": "seasons",
    "at": "automobile_types",
    "pc": "performance_categories",
    "ps": "price_segments",
    "y": "production_years",
    "tw": "tire_widths",
    "ar": "aspect_ratios",
    "rd": "rim_diameters",
    "si": "speed_indices",
    "li": "load_indices",
    "t": "sizes",
    "has_mode": "sizes",
    "np": "include_discontinued",
    "rf": "runflat_filter",
    "oe": "include_oe",
    "xl": "extra_load",
    "ms": "mud_and_snow",
    "nw": "nordic_winter",
    "type": "material_type",
    "year": "years",
    "price_segment": "price_segments",
    "show_discontinued": "include_discontinued",
    "show_oe": "include_oe",
    "q": "query",
}

_PARAM_HINTS = {
    "brand": "Use tires_list_brands to find valid brand slugs.",
    "brands": "Use tires_list_brands to find valid brand slugs.",
    "product": "Resolve the model via tires_search or tires_list_brand_tires first.",
    "region": "Use tires_list_regions to find valid region slugs.",
    "regions": "Use tires_list_regions to find valid region slugs.",
    "season": "Season slugs: 'summer', 'all', 'winter'.",
    "seasons": "Season slugs: 'summer', 'all', 'winter'.",
    "automobile_type": "Valid values: 'car', 'suv'.",
    "automobile_types": "Valid values: 'car', 'suv'.",
    "performance_categories": "Use tires_list_performance_categories to find valid slugs.",
    "price_segments": "Known segment slugs: 'premium', 'mid-range', 'economy'.",
    "slug": "Use tires_list_tests to find valid test slugs.",
    "sizes": "Pass tire size notation unchanged, e.g. '225/45R17'.",
}


# ---------------------------------------------------------------------------
# Secret redaction helpers (pure functions; client and response layers share)
# ---------------------------------------------------------------------------


def scrub_text(text: str, secrets: Collection[str] = ()) -> str:
    """Redact credential query params and known secret values from plain text."""
    text = _CRED_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def sanitize_url(url: str, secrets: Collection[str] = ()) -> str:
    """Return a safe-to-display URL: drop ``user_key`` and secret-valued params.

    Non-URL or unparseable input is still scrubbed of known secret values.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return scrub_text(url, secrets)
    if parts.query:

        def keep(pair: tuple[str, str]) -> bool:
            name, value = pair
            if name.lower() == "user_key":
                return False
            return not any(s and (s in value or s in name) for s in secrets)

        query = urlencode([p for p in parse_qsl(parts.query, keep_blank_values=True) if keep(p)])
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    return scrub_text(url, secrets)


def scrub_data(value: Any, secrets: Collection[str] = ()) -> Any:
    """Recursively redact secrets in a decoded JSON payload.

    Strings that look like URLs (absolute ``http(s)://`` or API-path relative)
    go through :func:`sanitize_url`; all other strings are scrubbed as text.
    """
    if isinstance(value, str):
        if value.startswith(("http://", "https://", API_PREFIX)):
            return sanitize_url(value, secrets)
        return scrub_text(value, secrets)
    if isinstance(value, Mapping):
        return {scrub_text(str(k), secrets): scrub_data(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_data(v, secrets) for v in value]
    return value


# ---------------------------------------------------------------------------
# Log hygiene: httpx logs full request URLs (including user_key) at INFO.
# A logger-level filter redacts credential params and registered secret values
# before any handler sees the record — independent of the user's log setup.
# ---------------------------------------------------------------------------

_log_filter_installed = False
_log_secrets: set[str] = set()


class _CredentialLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - formatting edge cases
            return True
        cleaned = scrub_text(rendered, _log_secrets)
        if cleaned != rendered:
            record.msg = cleaned
            record.args = ()
        return True


def _install_log_sanitizer(api_key: str) -> None:
    """Attach the credential-redacting filter to the httpx logger once."""
    global _log_filter_installed
    if api_key:
        _log_secrets.add(api_key)
    if not _log_filter_installed:
        logging.getLogger("httpx").addFilter(_CredentialLogFilter())
        logging.getLogger("httpcore").addFilter(_CredentialLogFilter())
        _log_filter_installed = True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TiresSettings:
    """Explicit, injectable client configuration.

    ``from_env()`` is the only place that reads process environment; callers
    (``create_server()``) choose when that happens. ``host_header`` exists solely
    for deliberately configured local routing (e.g. a dev gateway) and is sent
    verbatim as the HTTP ``Host`` header.

    The api key is excluded from ``repr()`` so settings are safe to print.
    Validation errors never echo the unsafe input (a base_url could itself
    contain credentials).
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False)
    host_header: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = MAX_RETRIES

    def __post_init__(self) -> None:
        base = str(self.base_url).strip()
        try:
            parts = urlsplit(base)
            port = parts.port  # raises ValueError on an invalid port
        except ValueError:
            port = None
            parts = None
        valid = (
            parts is not None
            and parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and parts.path in ("", "/")
            and not parts.query
            and not parts.fragment
            and (port is None or 0 < port < 65536)
        )
        if not valid:
            raise ValueError(
                "Invalid Tires API base_url: expected 'http(s)://host[:port]' "
                "with no credentials, path, query or fragment."
            )
        object.__setattr__(self, "base_url", base.rstrip("/") or base)

        host_header = str(self.host_header).strip()
        if host_header and not re.fullmatch(r"[A-Za-z0-9._:-]+", host_header):
            raise ValueError("Invalid host_header: expected a plain Host value like 'api.local'.")
        object.__setattr__(self, "host_header", host_header)

        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Invalid timeout: expected a finite positive number of seconds.")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool):
            raise ValueError("Invalid max_retries: expected an int between 0 and 2.")
        if not 0 <= self.max_retries <= MAX_RETRIES:
            raise ValueError(f"Invalid max_retries: expected 0..{MAX_RETRIES}.")

    @classmethod
    def from_env(cls) -> "TiresSettings":
        """Build settings from process environment (used by the CLI only)."""
        return cls(
            base_url=os.environ.get("TIRES_API_BASE_URL", DEFAULT_BASE_URL),
            api_key=os.environ.get("WHEELSIZE_API_KEY", ""),
            host_header=os.environ.get("TIRES_API_HOST_HEADER", ""),
        )


# ---------------------------------------------------------------------------
# Error formatting — all messages are built from scrubbed text only
# ---------------------------------------------------------------------------


def _validation_lines(payload: Any, secrets: Collection[str]) -> list[str]:
    """Render DRF-style error bodies: field lists, detail, non_field_errors, lists."""
    if isinstance(payload, list):
        return [f"- {scrub_text(str(m), secrets)[:200]}" for m in payload[:_MAX_ERROR_LINES]]
    if not isinstance(payload, dict):
        return []
    lines: list[str] = []
    for field_name, messages in payload.items():
        if field_name in ("detail", "code", "message"):
            continue  # handled by _detail_text
        if isinstance(messages, (list, tuple)):
            texts = [str(m) for m in messages]
        else:
            texts = [str(messages)]
        if field_name == "non_field_errors":
            lines.extend(f"- {scrub_text(t, secrets)[:200]}" for t in texts)
            continue
        mcp_name = _API_TO_MCP_PARAM.get(field_name, field_name)
        label = field_name if mcp_name == field_name else f"{field_name} ({mcp_name})"
        hint = _PARAM_HINTS.get(mcp_name, "")
        for t in texts:
            line = f"- {scrub_text(label, secrets)}: {scrub_text(t, secrets)[:200]}"
            if hint:
                line += f" {hint}"
            lines.append(line)
        if len(lines) >= _MAX_ERROR_LINES:
            break
    return lines[:_MAX_ERROR_LINES]


def _detail_text(payload: Any, body: str, secrets: Collection[str]) -> str:
    """Best-effort human-readable error detail — scrubbed, then truncated."""
    detail: Any = None
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message")
    elif isinstance(payload, list):
        detail = "; ".join(str(m) for m in payload[:5])
    if detail is None and body:
        detail = body
    if detail is None:
        return ""
    if isinstance(detail, str):
        return scrub_text(detail, secrets)[:_BODY_SNIPPET]
    return scrub_text(str(detail), secrets)[:_BODY_SNIPPET]


def _error_message(
    status: int, payload: Any, body: str, secrets: Collection[str], attempts: int
) -> str:
    """Build an actionable ToolError message for a non-2xx response."""
    detail = _detail_text(payload, body, secrets)
    if status in (401, 403):
        msg = (
            f"Authentication failed (HTTP {status}). Set a valid WHEELSIZE_API_KEY "
            "for tiresvote-mcp (the same key as for the Wheel Fitment API)."
        )
        return f"{msg} Upstream: {detail}" if detail else msg
    if status == 429:
        suffix = "s" if attempts != 1 else ""
        msg = (
            f"Rate limited by the Tires API (HTTP 429) after {attempts} attempt{suffix}. "
            "Wait a moment and retry."
        )
        return f"{msg} Upstream: {detail}" if detail else msg

    lines = _validation_lines(payload, secrets)
    if lines:
        return f"Invalid parameters (HTTP {status}):\n" + "\n".join(lines)

    if status == 404:
        return (
            f"Not found (HTTP 404): {detail or 'no detail'}. Check slugs — resolve them via "
            "tires_search, tires_list_brands or tires_list_tests first."
        )
    if detail:
        return f"Tires API error (HTTP {status}): {detail}"
    return f"Tires API error (HTTP {status}). Retry shortly; report if persistent."


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Exponential backoff, honoring a finite non-negative Retry-After (capped).

    Invalid, NaN, infinite or negative header values fall back to backoff —
    never into an unbounded or erroring sleep.
    """
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = math.nan
            if math.isfinite(delay) and delay >= 0:
                return min(delay, RETRY_AFTER_CAP)
    return BACKOFF_BASE * (2**attempt)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TiresClient:
    """Async HTTP client for the public Tires API.

    ``transport`` may be any ``httpx.AsyncBaseTransport`` (e.g. MockTransport)
    to fully inject HTTP behavior in tests; otherwise a pooled AsyncClient is
    created lazily per event loop. ``aclose()`` releases the pooled client and
    is wired into the server lifespan by ``create_server()``.
    """

    def __init__(
        self,
        settings: TiresSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or TiresSettings()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        _install_log_sanitizer(self.settings.api_key)

    @property
    def secrets(self) -> frozenset[str]:
        """Known secret values that must never appear in surfaced output."""
        return frozenset(s for s in (self.settings.api_key,) if s)

    @property
    def api_host(self) -> str:
        """Hostname of the configured API base URL (for link classification)."""
        return urlsplit(self.settings.base_url).hostname or ""

    @property
    def api_origin(self) -> str:
        """Normalized ``scheme://host[:port]`` origin of the configured API."""
        return self.settings.base_url

    def scrub(self, value: Any) -> Any:
        """Redact this client's secrets from a payload or string."""
        return scrub_data(value, self.secrets) if not isinstance(value, str) else scrub_text(value, self.secrets)

    def status(self) -> dict[str, Any]:
        """Safe configuration snapshot for the config://status resource."""
        return {
            "api_base_url": sanitize_url(self.settings.base_url, self.secrets),
            "api_key_configured": bool(self.settings.api_key),
            "host_header_configured": bool(self.settings.host_header),
        }

    async def aclose(self) -> None:
        """Close the pooled HTTP client (idempotent; called by server lifespan)."""
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

    # -- path & params -------------------------------------------------------

    def _build_path(self, path: str, path_params: Mapping[str, Any]) -> str:
        """Validate against the endpoint allowlist and substitute slug params.

        Only the approved ``/v2/tires/…`` templates are admitted — excluded
        routes (articles, top-charts, any other path) are refused before any
        substitution. Slugs must match ``[-A-Za-z0-9_]+``, so traversal,
        ``%``-encoding, query/fragment injection and separators are rejected.
        All messages are built from redacted values.
        """
        if not isinstance(path, str) or path not in ALLOWED_PATHS:
            raise ToolError(
                f"Rejected API path {scrub_text(repr(path), self.secrets)}: "
                "only the approved /v2/tires/ GET endpoints may be called."
            )
        placeholders = set(re.findall(r"\{([A-Za-z_]+)\}", path))
        unknown = set(path_params) - placeholders
        if unknown:
            raise ToolError(
                f"Path {scrub_text(repr(path), self.secrets)} has no parameters "
                f"{scrub_text(repr(sorted(unknown)), self.secrets)}: check the template name."
            )
        values: dict[str, str] = {}
        for name, value in path_params.items():
            text = value if isinstance(value, str) else str(value)
            if not _PATH_PARAM_RE.match(text):
                raise ToolError(
                    f"Invalid {scrub_text(repr(name), self.secrets)} path parameter "
                    f"{scrub_text(repr(text), self.secrets)}: expected a slug like "
                    "'bridgestone' ([a-zA-Z0-9_-]+)."
                )
            values[name] = text
        try:
            final = path.format(**values) if path_params else path
        except (KeyError, IndexError, ValueError):
            raise ToolError(
                f"Malformed API path template {scrub_text(repr(path), self.secrets)}"
            ) from None
        if not _PATH_RESULT_RE.match(final):
            raise ToolError(
                f"Rejected resolved API path {scrub_text(repr(final), self.secrets)}: "
                "invalid segment composition."
            )
        return final

    def _encode_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """Normalize query params: drop None, lowercase bools, keep lists.

        ``False`` is always preserved (it is a real filter value); only ``None``
        means 'parameter omitted'. List values become repeated query keys via
        httpx's standard encoding.
        """
        query: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                query[key] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                query[key] = [v if not isinstance(v, bool) else ("true" if v else "false") for v in value]
            else:
                query[key] = value
        if self.settings.api_key:
            query["user_key"] = self.settings.api_key
        return query

    def _get_client(self) -> httpx.AsyncClient:
        """Reuse one AsyncClient per event loop (connection pooling).

        When the loop changes (e.g. one loop per test), the stale client is
        closed on its own loop if that loop is still running, or detached —
        never silently left holding connections.
        """
        loop = asyncio.get_running_loop()
        stale = self._client
        if stale is not None and (stale.is_closed or self._loop is not loop):
            self._client = None
            if (
                not stale.is_closed
                and self._loop is not None
                and self._loop.is_running()
            ):
                # The stale client's transport must close on the loop that owns
                # it; schedule there best-effort, errors are unrecoverable here.
                with suppress(Exception):
                    asyncio.run_coroutine_threadsafe(stale.aclose(), self._loop)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.timeout, transport=self._transport
            )
            self._loop = loop
        return self._client

    # -- requests ------------------------------------------------------------

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        /,
        **path_params: Any,
    ) -> Any:
        """GET an approved ``/v2/tires/…`` endpoint and return scrubbed JSON.

        Args:
            path: A template from :data:`ALLOWED_PATHS`; ``{name}`` placeholders
                are filled from ``path_params`` after strict slug validation.
            params: Query parameters; ``user_key`` is added automatically.
            path_params: Slug values substituted into the path template.

        Returns:
            Decoded JSON payload with ``user_key`` params stripped from embedded
            URLs and known secret values redacted.

        Raises:
            ToolError: On non-2xx responses, transport failure, invalid JSON or
                a rejected path. Raised without chaining so request internals
                (URLs, headers) cannot leak through tracebacks.
        """
        url_path = self._build_path(path, path_params)
        query = self._encode_params(params)
        headers = {"Host": self.settings.host_header} if self.settings.host_header else {}
        client = self._get_client()

        attempts = self.settings.max_retries + 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = await client.get(
                    f"{self.settings.base_url}{url_path}",
                    params=query,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                if attempt == self.settings.max_retries:
                    raise ToolError(
                        scrub_text(
                            f"Network error reaching the Tires API: {exc}. Try again shortly.",
                            self.secrets,
                        )
                    ) from None
                response = None
            else:
                if response.status_code not in RETRY_STATUSES or attempt == self.settings.max_retries:
                    break
            await asyncio.sleep(_retry_delay(response, attempt))

        assert response is not None  # loop always assigns or raises
        if response.status_code != 200:
            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            raise ToolError(
                _error_message(
                    response.status_code, payload, response.text, self.secrets, attempts
                )
            ) from None

        try:
            data: Any = response.json()
        except ValueError:
            # Redact first, then truncate: a secret must never leak via a
            # snippet cut in the middle of its value.
            snippet = scrub_text(response.text, self.secrets)[:_BODY_SNIPPET]
            raise ToolError(
                f"Tires API returned HTTP 200 but the body is not valid JSON: {snippet!r}"
            ) from None
        return scrub_data(data, self.secrets)
