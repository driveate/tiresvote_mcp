"""Response unwrapping, compact projections and the two pagination policies.

Successful report responses have the upstream shape ``{data, meta}``; anything
else at HTTP 200 — a non-object payload, a missing ``data`` member, a
wrong-typed ``meta``/``pagination`` — is a contract violation and raises
``ToolError`` rather than being silently normalized.

Two pagination policies (see docs/api-knowledge.md):

* **API pages** (search and test lists): upstream returns ``meta.pagination``
  with absolute ``first``/``prev``/``next``/``last`` links. A ``next`` link is
  only *evidence* that another page exists: we never request it. ``has_more``
  is set only when the link stays on the same origin, on the exact endpoint
  path, without credentials, and carries the consecutive ``page`` number —
  navigation is expressed as ``next_page`` (an integer argument to the same
  tool). When upstream's ``next`` leaves the API — the search limit hands off
  to the TiresVote HTML site — navigation stops: ``pagination_limited`` is set
  and a sanitized ``site_url`` is surfaced only for allowlisted hosts.

* **MCP slices** (full lists and test participants): one upstream request,
  local ``limit``/``offset`` slicing. ``total`` is the upstream total when
  given, ``available_count`` the size of the actually fetched set, and
  ``truncated`` marks upstream-side loss (e.g. the 200-model catalog cap).

Nullability is preserved (absent evidence vs. empty list vs. negative finding),
and all surfaced links go through secret/``user_key`` redaction.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastmcp.exceptions import ToolError

from tiresvote_mcp.client import SITE_HOSTS, sanitize_url, scrub_data


def unwrap_report(payload: Any) -> tuple[Any, dict]:
    """Validate the upstream ``{data, meta}`` report envelope.

    Returns ``(data, meta)``. Missing or null ``meta`` is tolerated as ``{}``
    (some endpoints omit it), but a missing ``data`` member, a non-object
    payload, or a present-but-wrong-type ``meta`` is a contract violation.
    """
    if not isinstance(payload, dict) or "data" not in payload:
        raise ToolError(
            "Unexpected Tires API response shape: expected a JSON object with a 'data' member. "
            "The upstream contract may have changed."
        )
    meta = payload.get("meta")
    if meta is None:
        return payload["data"], {}
    if not isinstance(meta, dict):
        raise ToolError(
            "Unexpected Tires API response shape: 'meta' must be an object, "
            f"got {type(meta).__name__}."
        )
    return payload["data"], meta


def _require_list(data: Any, what: str) -> list:
    if not isinstance(data, list):
        raise ToolError(
            f"Unexpected Tires API response shape: 'data' for {what} must be a list, "
            f"got {type(data).__name__}."
        )
    return data


def _classify_next_link(
    url: Any, *, api_base_url: str, expected_path: str, page: int
) -> tuple[str, str | None]:
    """Classify an upstream pagination ``next`` link.

    Returns ``("api", None)`` only when the link stays on the configured origin
    (identical scheme/host/port for absolute links; path-only relative links
    inherit the request origin), hits the *exact* endpoint path, carries no
    credentials, and requests the consecutive numeric page (``page + 1``).
    Returns ``("site", url)`` for allowlisted TiresVote HTML pages, and
    ``("other", None)`` for anything else — including scheme-relative links.
    In every case the URL is classified, never fetched; only allowlisted site
    links are ever surfaced.
    """
    if not isinstance(url, str) or not url.strip():
        return "other", None
    try:
        parts = urlsplit(url)
        expected = urlsplit(api_base_url)
        link_port = parts.port  # raises ValueError on a bad port
    except ValueError:
        return "other", None

    is_relative = not parts.netloc and not parts.scheme
    if not is_relative:
        # Absolute link: same origin requires identical scheme, host and port
        # (no downgrade, no port trickery) and no embedded credentials.
        if parts.scheme not in ("http", "https"):
            return "other", None
        if parts.username is not None or parts.password is not None:
            return "other", None
        if (
            parts.scheme == expected.scheme
            and parts.hostname == expected.hostname
            and link_port == expected.port
        ):
            same_origin = True
        else:
            same_origin = False
    else:
        same_origin = True  # relative link inherits the request origin

    if same_origin and parts.path == expected_path:
        pages = [v for k, v in parse_qsl(parts.query) if k == "page"]
        if len(pages) == 1 and pages[0].isdigit() and int(pages[0]) == page + 1:
            return "api", None
        return "other", None  # next exists but is not a clean consecutive page

    if parts.hostname in SITE_HOSTS and parts.scheme in ("http", "https"):
        return "site", url
    return "other", None


def api_page_envelope(
    payload: Any,
    *,
    page: int,
    per_page: int,
    map_item: Callable[[Any], dict],
    api_base_url: str,
    expected_path: str,
    secrets: Collection[str] = (),
) -> dict:
    """Build the API-pages envelope for a fetched upstream page.

    ``page``/``per_page`` echo the request. ``has_more`` means a further *API*
    page exists; ``next_page`` is the numeric argument for the next call — the
    upstream ``next`` URL itself is never requested or surfaced. When upstream
    links out to the HTML site (search pagination cap) or returns a link that
    fails origin/path/page validation, ``pagination_limited`` is true; a
    sanitized ``site_url`` is included only for allowlisted TiresVote hosts.

    ``api_base_url`` is the configured API origin and ``expected_path`` the
    endpoint's own path — continuation is authorized only against that pair.
    """
    data, meta = unwrap_report(payload)
    items = _require_list(data, "a paginated endpoint")
    pagination = meta.get("pagination")
    if pagination is None:
        pagination = {}
    elif not isinstance(pagination, dict):
        raise ToolError(
            "Unexpected Tires API response shape: 'meta.pagination' must be an object, "
            f"got {type(pagination).__name__}."
        )

    envelope: dict[str, Any] = {
        "results": [map_item(item) for item in items],
        "page": page,
        "per_page": per_page,
        "current_page_count": pagination.get("current_page_count", len(items)),
        "total_items": pagination.get("total_items"),
        "total_pages": pagination.get("total_pages"),
        "has_more": False,
        "pagination_limited": False,
    }

    next_link = pagination.get("next")
    if next_link is None:
        return envelope

    kind, safe_url = _classify_next_link(
        next_link, api_base_url=api_base_url, expected_path=expected_path, page=page
    )
    if kind == "api":
        envelope["has_more"] = True
        envelope["next_page"] = page + 1
        envelope["hint"] = f"Call again with page={page + 1} for the next page."
    elif kind == "site":
        envelope["pagination_limited"] = True
        envelope["site_url"] = sanitize_url(safe_url or "", secrets)
        envelope["hint"] = "Upstream pagination limit reached; continue on the TiresVote site."
    else:
        envelope["pagination_limited"] = True
        envelope["hint"] = "Upstream returned an unrecognized next-page link; API navigation stopped."
    return envelope


def slice_envelope(
    payload: Any,
    *,
    limit: int,
    offset: int,
    map_item: Callable[[Any], dict],
    truncated_hint: str | None = None,
) -> dict:
    """Build the MCP-slice envelope for a complete upstream list.

    ``total`` mirrors the upstream ``meta.count`` when present (it can exceed
    the fetched set — e.g. the 200-model catalog cap); ``available_count`` is
    the number of items actually received. ``truncated`` marks that upstream
    loss; ``has_more``/``next_offset`` navigate within the available set only.
    """
    data, meta = unwrap_report(payload)
    items = _require_list(data, "a list endpoint")

    available = len(items)
    total_raw = meta.get("count")
    total = total_raw if isinstance(total_raw, int) and not isinstance(total_raw, bool) else available
    truncated = total > available

    results = [map_item(item) for item in items[offset : offset + limit]]

    envelope: dict[str, Any] = {
        "results": results,
        "total": total,
        "available_count": available,
        "limit": limit,
        "offset": offset,
        "has_more": False,
        "truncated": truncated,
    }
    if offset + len(results) < available:
        envelope["has_more"] = True
        envelope["next_offset"] = offset + len(results)
        envelope["hint"] = f"Call again with offset={offset + len(results)} for the next slice."
    if truncated:
        envelope["hint"] = truncated_hint or (
            "The upstream list was truncated by the API; narrow the filters to reach the rest."
        )
    return envelope


def sanitized(value: Any, secrets: Collection[str]) -> Any:
    """Convenience alias for recursive secret redaction in tool mappers."""
    return scrub_data(value, secrets)


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def map_brand(item: Any) -> dict:
    """Project a /catalog/ brand row: keeps slug, display, price_segment, count.

    ``slug`` and ``display`` are required identity — a row without them is a
    contract violation, never an all-null brand. ``price_segment`` is a nullable
    display string upstream — ``null`` is preserved so a missing segment stays
    distinguishable from a known one.
    """
    if not isinstance(item, dict):
        raise ToolError("Unexpected brand row shape in Tires API response (expected object).")
    slug, display = item.get("slug"), item.get("display")
    if not isinstance(slug, str) or not slug or not isinstance(display, str) or not display:
        raise ToolError("Unexpected brand row shape in Tires API response (missing slug/display).")
    return {
        "slug": slug,
        "display": display,
        "price_segment": item.get("price_segment"),
        "products_count": item.get("products_count"),
    }
