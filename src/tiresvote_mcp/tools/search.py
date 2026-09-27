"""Search tools — text search and parametric advanced search.

Both tools use the API-pages pagination policy: ``page``/``per_page`` are sent
upstream and the envelope exposes a numeric ``next_page``; upstream ``next``
URLs are never requested. When upstream hands off to the TiresVote HTML site,
``pagination_limited`` is set and only a sanitized link on an allowlisted host
is surfaced.

Live-verified flag semantics (2026-09-27, docs/api-knowledge.md): on
``/search/advanced/`` the include flags ``np`` and ``oe`` are exposed as
``include_discontinued``/``include_oe`` — ``true`` widens the result set and
``false`` behaves like omission. The RunFlat flag ``rf`` is deliberately
asymmetric and exposed as the neutral ``runflat_filter``: ``true`` adds
RunFlat models, but explicit ``false`` does NOT exclude them (upstream
variant matching); only omission applies the default that excludes them.
The brand-catalog ``runflat`` filter on ``tires_list_brand_tires`` is a
different parameter where ``true`` means RunFlat-ONLY.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from tiresvote_mcp.client import TiresClient
from tiresvote_mcp.projections import ensure_within_budget, map_search_row
from tiresvote_mcp.response import api_page_envelope
from tiresvote_mcp.tools._annotations import SEARCH_ANNOTATIONS
from tiresvote_mcp.tools._scope import require_client
from tiresvote_mcp.tools.catalog import _nonempty

# Upstream validates production years as 1900..current_year+2 (snapshot: 2028).
_MAX_YEAR = date.today().year + 2

_PAGE = Annotated[int, Field(ge=1, description="API page number (>=1). Default 1.")]
_PER_PAGE = Annotated[
    int,
    Field(ge=1, le=20, description="Results per API page (1–20). Default 10."),
]


def register(mcp: FastMCP, client: TiresClient | None = None) -> None:
    """Register search tools. ``client`` defaults to the create_server scope."""
    client = require_client(client)

    @mcp.tool(annotations=SEARCH_ANNOTATIONS, tags={"search"})
    async def tires_search(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                description="Free-text model search, max 100 characters, e.g. 'pilot sport'. "
                "Use it to resolve a name into brand+product slugs.",
            ),
        ],
        page: _PAGE = 1,
        per_page: _PER_PAGE = 10,
    ) -> dict:
        """Search tire models by name.

        Rows carry ``slug``/``display``, ``brand`` slug, ``canonical_link``,
        season/automobile-type slugs, ``year``, status flags, ``counters``,
        region slugs, ``rating`` (``score`` = CoreScore and ``popularity`` as
        separate metrics) and ``has_modes`` (always ``{}`` here — this
        endpoint has no sizes filter).

        Unlike ``tires_search_advanced`` this search applies no
        discontinued/RunFlat/OE defaults — discontinued models may appear in
        results (live-observed).

        Follow ``next_page`` while ``has_more`` — never request a returned
        URL. If ``pagination_limited`` is true the upstream paging cap was
        reached; ``site_url`` (when present) is a citation link to the
        TiresVote site, not a fetch target. Per-row ``regions`` and
        ``rating.tags`` are capped (``regions_more``/``tags_more`` count the
        rest; the model's ``canonical_link`` is the complete-record route).
        Responses cap at ~40 KB serialized (roughly 8–10k tokens) — lower
        ``per_page`` if a page overflows.
        """
        payload = await client.get(
            "/v2/tires/search/",
            {"query": query.strip(), "page": page, "per_page": per_page},
        )
        return ensure_within_budget(
            api_page_envelope(
                payload,
                page=page,
                per_page=per_page,
                map_item=map_search_row,
                api_base_url=client.api_origin,
                expected_path="/v2/tires/search/",
                secrets=client.secrets,
            )
        )

    @mcp.tool(annotations=SEARCH_ANNOTATIONS, tags={"search"})
    async def tires_search_advanced(
        brands: Annotated[
            list[str] | None,
            Field(
                max_length=20,
                description="Brand slugs, e.g. ['michelin']. Resolve via "
                "tires_list_brands. Sent as repeated 'b' params "
                "(at most 20 — an MCP-side bound).",
            ),
        ] = None,
        regions: Annotated[
            list[str] | None,
            Field(
                max_length=20,
                description="TiresVote market region slugs, e.g. ['eudm']. Resolve via "
                "tires_list_regions. Sent as repeated 'reg' params "
                "(at most 20 — an MCP-side bound).",
            ),
        ] = None,
        seasons: Annotated[
            list[Literal["summer", "all", "winter"]] | None,
            Field(
                max_length=20,
                description="Season slugs: 'summer', 'all' (all-season), 'winter'. "
                "Sent as repeated 's' params.",
            ),
        ] = None,
        automobile_types: Annotated[
            list[Literal["car", "suv"]] | None,
            Field(
                max_length=20,
                description="Vehicle classes: 'car' and/or 'suv'. "
                "Sent as repeated 'at' params.",
            ),
        ] = None,
        performance_categories: Annotated[
            list[str] | None,
            Field(
                max_length=20,
                description="Performance category slugs. Resolve via "
                "tires_list_performance_categories. Sent as repeated 'pc' params "
                "(at most 20 — an MCP-side bound).",
            ),
        ] = None,
        price_segments: Annotated[
            list[str] | None,
            Field(
                max_length=20,
                description="Brand price-segment slugs, e.g. "
                "['premium','mid-range','economy']. Sent as repeated 'ps' params "
                "(at most 20 — an MCP-side bound).",
            ),
        ] = None,
        production_years: Annotated[
            list[Annotated[int, Field(ge=1900, le=_MAX_YEAR)]] | None,
            Field(
                max_length=20,
                description=f"Model production start years, 1900–{_MAX_YEAR}, e.g. [2020, 2021]. "
                "Sent as repeated 'y' params (at most 20 values — an MCP-side bound)."
            ),
        ] = None,
        tire_widths: Annotated[
            list[Annotated[int, Field(ge=95, le=525)]] | None,
            Field(
                max_length=20,
                description="Tire widths in mm, 95–525, e.g. [205, 225]. Sent as "
                "repeated 'tw' params (at most 20 — an MCP-side bound). Combined "
                "with other size fields on a single variant.",
            ),
        ] = None,
        aspect_ratios: Annotated[
            list[Annotated[int, Field(ge=20, le=95)]] | None,
            Field(
                max_length=20,
                description="Aspect ratios in %, 20–95, e.g. [45, 55]. Sent as "
                "repeated 'ar' params (at most 20 — an MCP-side bound). Combined "
                "with other size fields on a single variant.",
            ),
        ] = None,
        rim_diameters: Annotated[
            list[Annotated[int, Field(ge=10, le=32)]] | None,
            Field(
                max_length=20,
                description="Rim diameters in whole inches, 10–32, e.g. [17]. Sent "
                "as repeated 'rd' params (at most 20 — an MCP-side bound). "
                "Combined with other size fields on a single variant.",
            ),
        ] = None,
        speed_indices: Annotated[
            list[str] | None,
            Field(
                max_length=20,
                description="Exact speed indices, e.g. ['V','W','Y'] — each value "
                "matches exactly, not a minimum rating. Sent as repeated 'si' "
                "params (at most 20 — an MCP-side bound).",
            ),
        ] = None,
        load_indices: Annotated[
            list[Annotated[int, Field(ge=0, le=150)]] | None,
            Field(
                max_length=20,
                description="Exact load indices 0–150, e.g. [91, 94] — each value "
                "matches exactly, not a minimum rating. Sent as repeated 'li' "
                "params (at most 20 — an MCP-side bound).",
            ),
        ] = None,
        sizes: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=50)]] | None,
            Field(
                max_length=20,
                description="Full tire size notations in original spelling, each up to 50 chars "
                "(an MCP-side bound), e.g. ['225/45R17'] or a staggered pair "
                "['245/40R19','275/35R19']. Sent as repeated 't' params (at most 20). Multiple "
                "values are OR alternatives — a hit for one size does not prove the others exist on "
                "the model; verify each size via tires_list_sizes."
            ),
        ] = None,
        include_discontinued: Annotated[
            bool | None,
            Field(
                description="true adds discontinued models to the results (live-verified include "
                "flag, sent as 'np'); false matches omitting it. Omitted default excludes "
                "discontinued models."
            ),
        ] = None,
        runflat_filter: Annotated[
            bool | None,
            Field(
                description="Neutral RunFlat visibility flag, sent as 'rf' (live-verified): omit "
                "to keep the upstream default, which excludes RunFlat-flagged models; true adds "
                "RunFlat models; explicit false ALSO returns RunFlat models due to upstream "
                "variant matching — it is not an exclusion. For a strictly RunFlat-only list use "
                "tires_list_brand_tires(runflat=true)."
            ),
        ] = None,
        include_oe: Annotated[
            bool | None,
            Field(
                description="true adds OE (original equipment) models to the results "
                "(live-verified include flag, sent as 'oe'); false matches omitting it."
            ),
        ] = None,
        extra_load: Annotated[
            bool | None,
            Field(description="Variant attribute: true requires XL variants. Sent as 'xl'."),
        ] = None,
        mud_and_snow: Annotated[
            bool | None,
            Field(description="Variant attribute: true requires M+S variants. Sent as 'ms'."),
        ] = None,
        nordic_winter: Annotated[
            bool | None,
            Field(description="Model attribute: true keeps models intended for Nordic winter. Sent as 'nw'."),
        ] = None,
        ordering: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=100,
                description="Sort order, comma-separated without spaces; fields: 'popularity', "
                "'score', 'slug', optional '-' prefix. Upstream default: '-popularity,-score,slug'.",
            ),
        ] = None,
        page: _PAGE = 1,
        per_page: _PER_PAGE = 10,
    ) -> dict:
        """Search tire models by filters instead of a name.

        All filters are optional; every list maps to repeated query params
        (values inside one list are alternatives). Size fields
        (``tire_widths``, ``aspect_ratios``, ``rim_diameters``,
        ``speed_indices``, ``load_indices``, ``sizes``, ``extra_load``,
        ``mud_and_snow``) constrain ONE variant — a model matches when a
        single variant satisfies them together; do not combine evidence from
        different variants. ``nordic_winter`` is a model-level attribute.
        ``sizes`` values are OR alternatives, not a required set: for a
        staggered pair, verify EACH size separately on the chosen model via
        ``tires_list_sizes``.

        Rows add ``has_modes``: for each requested ``sizes`` value either the
        list of matched variant designations or ``null`` — a hint for
        verification, not proof (a null means 'no matched designation
        recorded', and ``{}`` means no sizes were requested). Per-size lists
        are capped at 8 designations (``_truncated_sizes`` names the affected
        sizes) — ``tires_list_sizes`` returns a model's complete variant
        list. ``rating.score`` is CoreScore, ``rating.popularity`` is a
        separate metric.

        Pagination: follow ``next_page`` while ``has_more``; upstream caps how
        deep paging goes — a short listing does not prove absence, and a
        ``site_url`` link is a citation, not a fetch target. Responses cap at
        ~40 KB serialized (roughly 8–10k tokens) — lower ``per_page`` or
        narrow filters if a page overflows.
        """
        payload = await client.get(
            "/v2/tires/search/advanced/",
            {
                "b": _nonempty(brands, "brands", "['michelin']"),
                "reg": _nonempty(regions, "regions", "['eudm']"),
                "s": _nonempty(seasons, "seasons", "['summer']"),
                "at": _nonempty(automobile_types, "automobile_types", "['car']"),
                "pc": _nonempty(
                    performance_categories,
                    "performance_categories",
                    "['car-max-performance-summer']",
                ),
                "ps": _nonempty(price_segments, "price_segments", "['premium']"),
                "y": _nonempty(production_years, "production_years", "[2020]"),
                "tw": _nonempty(tire_widths, "tire_widths", "[205]"),
                "ar": _nonempty(aspect_ratios, "aspect_ratios", "[45]"),
                "rd": _nonempty(rim_diameters, "rim_diameters", "[17]"),
                "si": _nonempty(speed_indices, "speed_indices", "['Y']"),
                "li": _nonempty(load_indices, "load_indices", "[91]"),
                "t": _nonempty(sizes, "sizes", "['225/45R17']"),
                "np": include_discontinued,
                "rf": runflat_filter,
                "oe": include_oe,
                "xl": extra_load,
                "ms": mud_and_snow,
                "nw": nordic_winter,
                "ordering": ordering,
                "page": page,
                "per_page": per_page,
            },
        )
        return ensure_within_budget(
            api_page_envelope(
                payload,
                page=page,
                per_page=per_page,
                map_item=map_search_row,
                api_base_url=client.api_origin,
                expected_path="/v2/tires/search/advanced/",
                secrets=client.secrets,
            )
        )
