"""Compact per-endpoint projections and the response-size guard.

Each mapper keeps the contract-relevant fields of an upstream object and drops
the rest, so a full page of results stays inside the response budget.
Projections are deliberately explicit — small dict builders instead of generic
schema transforms.

Rules (see docs/api-knowledge.md):

* ``None`` stays ``None`` — upstream distinguishes absent evidence (null)
  from an empty collection; mappers never coerce one into the other. A
  missing/!list member where the contract requires a list is a contract
  violation (``ToolError``), not an empty result.
* Essential identity is required: a row without ``slug``/``text`` cannot be
  surfaced, because an all-null card looks like valid data but is not.
* Nested lists and long text fields are capped with explicit markers
  (``*_more`` counts, ``*_truncated: true``, inline ``…``). Nothing is cut
  silently, and every cut names a continuation route (a citation link or
  another tool) in the tool docstrings.
* Upstream text (descriptions, reason text, material leads, verdicts) is
  untrusted data — bounded here, never interpreted.
* ``rating.score`` (CoreScore), ``rating.popularity`` and ``test_score`` are
  three distinct metrics and are kept as separate fields.

``ensure_within_budget`` is the last-resort guard: a serialized response over
``MAX_RESPONSE_BYTES`` fails with an actionable error instead of silently
dropping identifiers or emitting unbounded output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastmcp.exceptions import ToolError

# -- bounds -----------------------------------------------------------------

# Serialized-size ceiling for one tool response. ~40 KB of JSON is roughly
# 8–10k tokens for typical catalog text — a target, not an exact token count.
MAX_RESPONSE_BYTES = 40_000

MAX_REGION_REFS = 10  # region slugs per row; extras reported via regions_more
MAX_RATING_TAGS = 8  # per rating block; extras via tags_more
MAX_HAS_MODES_PER_SIZE = 8  # matched designations per requested size
MAX_FAMILY_LINKS = 10  # successors / runflat_models
MAX_TAGS = 12  # card/material/benchmark/participant tag lists

DESCRIPTION_MAX = 4000  # product detail 'full' mode
SUMMARY_MAX = 400  # material leads / link text / benchmark descriptions
VERDICT_MAX = 600  # test-participant verdict text
REASON_MAX = 400  # buy/not-buy reason text
TITLE_MAX = 200  # material/test titles
DISPLAY_MAX = 120  # display labels
TEXT_MAX = 120  # variant designation text
COUNTRY_MAX = 120  # a single country name


def _clip(text: Any, limit: int) -> Any:
    """Hard-cap a short label (display, title, country) with a ``…`` marker."""
    if not isinstance(text, str):
        return text
    return text if len(text) <= limit else text[:limit] + "…"


def _truncate(text: Any, limit: int, *, what: str = "text") -> str | None:
    """Bound a long text field; ``None`` stays ``None``.

    A present non-string value is a contract violation, not silently nulled.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise ToolError(
            f"Unexpected Tires API response shape: {what} must be a string or "
            f"null, got {type(text).__name__}."
        )
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated at {limit} chars]"


def _truncated_flag(text: Any, limit: int) -> bool:
    return isinstance(text, str) and len(text) > limit


def _cap(value: Any, limit: int, *, what: str = "list") -> tuple[list | None, int]:
    """Slice a list; returns ``(kept, omitted_count)``.

    ``None`` input yields ``(None, 0)`` so callers can emit ``null`` —
    absent evidence is not an empty collection. A present non-list value is a
    contract violation, never silently emptied.
    """
    if value is None:
        return None, 0
    if not isinstance(value, list):
        raise ToolError(
            f"Unexpected Tires API response shape: expected a {what} list or null, "
            f"got {type(value).__name__}."
        )
    return value[:limit], max(0, len(value) - limit)


def _require_obj(item: Any, what: str) -> Mapping:
    if not isinstance(item, Mapping):
        raise ToolError(
            f"Unexpected Tires API response shape: expected {what} object, "
            f"got {type(item).__name__}."
        )
    return item


def _require_slug(item: Mapping, what: str) -> Mapping:
    """Essential identity: a row without a non-empty slug is malformed."""
    if not isinstance(item.get("slug"), str) or not item["slug"]:
        raise ToolError(
            f"Unexpected Tires API response shape: {what} row is missing its 'slug'."
        )
    return item


def _tag_slice(
    value: Any, *, key: str, out: dict, limit: int = MAX_TAGS
) -> None:
    """Emit ``key`` (list|null preserved) plus ``key_more`` when capped."""
    items, omitted = _cap(value, limit, what=f"'{key}'")
    out[key] = [_clip(t, TITLE_MAX) if isinstance(t, str) else t for t in items] \
        if items is not None else None
    if omitted:
        out[f"{key}_more"] = omitted


# -- shared small pieces ------------------------------------------------------


def map_named_ref(value: Any) -> dict | None:
    """``{slug, display}`` references (brand, season, category, …).

    ``null`` is preserved; a present but non-object value is malformed. A
    reference object with ``null`` slug/display is legitimate upstream data
    (season-agnostic categories).
    """
    if value is None:
        return None
    value = _require_obj(value, "reference")
    return {"slug": value.get("slug"), "display": _clip(value.get("display"), DISPLAY_MAX)}


def _slug_of(value: Any) -> Any:
    """Reduce a ``{slug, display}`` reference to its slug identifier."""
    if isinstance(value, Mapping):
        return value.get("slug")
    return value


def map_rating(value: Any) -> dict | None:
    """CoreScore/Popularity plus a bounded tag list. ``null`` preserved.

    ``score`` and ``popularity`` are separate metrics; tags are clipped to
    ``MAX_RATING_TAGS`` with a ``tags_more`` count — the model's
    ``canonical_link`` (on the row/card) is the full-list route.
    """
    if value is None:
        return None
    value = _require_obj(value, "rating")
    tags, omitted = _cap(value.get("tags"), MAX_RATING_TAGS, what="rating 'tags'")
    out = {
        "score": value.get("score"),
        "popularity": value.get("popularity"),
        "tags": [
            {
                "slug": t.get("slug") if isinstance(t, Mapping) else t,
                "display": _clip(t.get("display"), DISPLAY_MAX)
                if isinstance(t, Mapping)
                else None,
                "connotation": t.get("connotation") if isinstance(t, Mapping) else None,
            }
            for t in (tags or [])
        ]
        if tags is not None
        else None,
    }
    if omitted:
        out["tags_more"] = omitted
    return out


def _region_slugs(value: Any) -> tuple[list | None, int]:
    """Region objects → bare slug list (slugs are the identifiers).

    ``null`` regions stay ``null`` — absent region data is not an empty list;
    a present non-list is a contract violation, not emptied evidence.
    """
    if value is None:
        return None, 0
    kept, omitted = _cap(value, MAX_REGION_REFS, what="'regions'")
    return [r.get("slug") if isinstance(r, Mapping) else r for r in kept], omitted


def _put_regions(out: dict, value: Any) -> None:
    slugs, omitted = _region_slugs(value)
    out["regions"] = slugs
    if omitted:
        out["regions_more"] = omitted


def map_has_modes(value: Any) -> dict | None:
    """``has_modes``: requested size → matched designations (list) or null.

    The Swagger snapshot wrongly types this as a boolean map; the real shape
    is ``{size: [str, ...] | null}``, or ``{}`` when no sizes were requested.
    A boolean or otherwise malformed member is a contract violation, not data
    to coerce. Lists are capped per size with a ``_truncated_sizes`` marker —
    ``tires_list_sizes`` is the complete-list route.
    """
    if value is None:
        return None
    value = _require_obj(value, "has_modes")
    out: dict[str, Any] = {}
    truncated: list[str] = []
    for size, modes in value.items():
        if modes is None:
            out[size] = None  # no match recorded for this size
        elif isinstance(modes, list) and all(isinstance(m, str) for m in modes):
            kept, omitted = _cap(modes, MAX_HAS_MODES_PER_SIZE)
            out[size] = [_clip(m, TEXT_MAX) for m in kept]
            if omitted:
                truncated.append(size)
        else:
            raise ToolError(
                f"Unexpected Tires API response shape: has_modes[{size!r}] must be "
                f"a list of designations or null, got {type(modes).__name__}."
            )
    if truncated:
        out["_truncated_sizes"] = truncated
    return out


def map_family_ref(value: Any) -> dict | None:
    """``{brand_slug, product_slug, display_name}`` navigation link."""
    if value is None:
        return None
    value = _require_obj(value, "model relation")
    return {
        "brand": value.get("brand_slug"),
        "product": value.get("product_slug"),
        "display": _clip(value.get("display_name"), DISPLAY_MAX),
    }


def _family_list(out: dict, key: str, value: Any) -> None:
    """Emit ``key`` (list|null preserved) plus ``key_more`` when capped."""
    items, omitted = _cap(value, MAX_FAMILY_LINKS, what=f"'{key}'")
    out[key] = [map_family_ref(v) for v in items] if items is not None else None
    if omitted:
        out[f"{key}_more"] = omitted


def _counters(value: Any) -> dict | None:
    """Project the documented counters (modes/videos/benchmarks) verbatim.

    Values are kept as returned — ``counters.modes`` may serialize as a string
    upstream; no numeric coercion. ``modes`` is only a hint: 0 does not prove
    a model has no variants.
    """
    if value is None:
        return None
    value = _require_obj(value, "counters")
    return {
        k: value.get(k)
        for k in ("modes", "videos", "benchmarks")
        if k in value
    }


def ensure_within_budget(payload: Any, *, budget: int = MAX_RESPONSE_BYTES) -> Any:
    """Fail rather than emit an oversized response.

    Returns ``payload`` unchanged when its serialized size fits ``budget``;
    otherwise raises a ``ToolError`` telling the caller to reduce
    limit/per_page or narrow filters — bounded output must never silently
    drop identifiers or citations.
    """
    try:
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ToolError(f"Tool produced a non-serializable response: {exc}") from exc
    if size > budget:
        raise ToolError(
            f"Response exceeded the {budget}-byte compactness budget "
            f"({size} bytes serialized). Reduce limit/per_page or narrow the "
            "filters and retry; no data was silently dropped."
        )
    return payload


# -- catalog / search rows ------------------------------------------------------


def map_product_row(item: Any) -> dict:
    """Compact catalog/search row: identity, status flags, counters, regions."""
    item = _require_slug(_require_obj(item, "product row"), "product row")
    out: dict[str, Any] = {
        "slug": item["slug"],
        "display": _clip(item.get("display"), DISPLAY_MAX),
        "brand": _slug_of(item.get("brand")),
        "canonical_link": item.get("canonical_link"),
        "season": _slug_of(item.get("season")),
        "automobile_type": _slug_of(item.get("automobile_type")),
        "year": item.get("year"),
        "discontinued": item.get("discontinued"),
        "almost_discontinued": item.get("almost_discontinued"),
        "coming_soon": item.get("coming_soon"),
        "is_runflat": item.get("is_runflat"),
        "counters": _counters(item.get("counters")),
    }
    _put_regions(out, item.get("regions"))
    return out


def map_search_row(item: Any) -> dict:
    """Search/advanced row: product row plus rating and ``has_modes``.

    ``has_modes`` maps each requested ``sizes`` value to the matched variant
    designations — a list, ``null`` (no matched designation recorded for that
    size) or ``{}`` when no sizes were requested. The ``sizes`` filter is an
    OR across values; ``has_modes`` is a hint, not proof — verify each size
    via ``tires_list_sizes``.
    """
    out = map_product_row(item)
    out["rating"] = map_rating(item.get("rating"))
    out["has_modes"] = map_has_modes(item.get("has_modes"))
    return out


def map_product_card(item: Any, meta: Mapping, *, full: bool) -> dict:
    """Product detail projection. ``full`` adds description, tags and image.

    ``description``/``tags`` are bounded; when cut, ``*_truncated``/``*_more``
    markers point to the ``canonical_link`` already on the card as the
    complete-record route.
    """
    item = _require_slug(_require_obj(item, "product"), "product")
    out: dict[str, Any] = {
        "slug": item["slug"],
        "display": _clip(item.get("display"), DISPLAY_MAX),
        "brand": map_named_ref(item.get("brand")),
        "canonical_link": item.get("canonical_link"),
        "season": map_named_ref(item.get("season")),
        "automobile_type": map_named_ref(item.get("automobile_type")),
        "performance_category": map_named_ref(item.get("performance_category")),
        "year": item.get("year"),
        "discontinued": item.get("discontinued"),
        "almost_discontinued": item.get("almost_discontinued"),
        "coming_soon": item.get("coming_soon"),
        "is_runflat": item.get("is_runflat"),
        "studded": item.get("studded"),
        "for_nordic_winter": item.get("for_nordic_winter"),
        "is_oe_model": item.get("is_oe_model"),
        "manufacturer_page_link": item.get("manufacturer_page_link"),
        "rating": map_rating(item.get("rating")),
        "counters": _counters(item.get("counters")),
        "regions": map_named_regions(item.get("regions")),
        "has_bnb_reasons": item.get("has_bnb_reasons"),
        "ancestor": map_family_ref(item.get("ancestor")),
        "last_update": meta.get("last_update") if isinstance(meta, Mapping) else None,
    }
    _family_list(out, "successors", item.get("successors"))
    _family_list(out, "runflat_models", item.get("runflat_models"))
    if full:
        description = item.get("description")
        out["description"] = _truncate(description, DESCRIPTION_MAX)
        if _truncated_flag(description, DESCRIPTION_MAX):
            out["description_truncated"] = True
        _tag_slice(item.get("tags"), key="tags", out=out, limit=MAX_TAGS)
        out["image"] = item.get("image")
    return out


def map_named_regions(value: Any) -> dict | None:
    """Regions with ``{slug, display}`` — used on single-object responses.

    Returns ``{"items": [...], "more": n?}`` so an omitted tail is reported;
    the model ``canonical_link`` is the complete-list route. ``None`` stays
    ``None``; a present non-list is a contract violation.
    """
    if value is None:
        return None
    items, omitted = _cap(value, MAX_REGION_REFS, what="'regions'")
    out: dict[str, Any] = {"items": [map_named_ref(r) for r in items or []]}
    if omitted:
        out["more"] = omitted
    return out


def map_mode(item: Any) -> dict:
    """A size variant (``/modes/``). Decimal geometry and units are preserved.

    ``sizing_system`` selects which dimension fields carry values:
    metric/lt-metric use ``tire_width`` (mm), ``aspect_ratio`` and
    ``rim_diameter`` (inches); flotation/lt-numeric use ``overall_diameter``,
    ``section_width`` and ``rim_diameter`` (inches). Empty upstream values
    serialize as ``null`` and stay ``null``.
    """
    item = _require_obj(item, "variant")
    text = item.get("text")
    if not isinstance(text, str) or not text:
        raise ToolError(
            "Unexpected Tires API response shape: variant row is missing its 'text' designation."
        )
    return {
        "sizing_system": item.get("sizing_system"),
        "text": _clip(text, TEXT_MAX),
        "tire_width": item.get("tire_width"),
        "aspect_ratio": item.get("aspect_ratio"),
        "rim_diameter": item.get("rim_diameter"),
        "overall_diameter": item.get("overall_diameter"),
        "section_width": item.get("section_width"),
        "load_index": item.get("load_index"),
        "dual_load_index": item.get("dual_load_index"),
        "speed_index": item.get("speed_index"),
        "extra_load": item.get("extra_load"),
        "mud_and_snow": item.get("mud_and_snow"),
        "rim_protection": item.get("rim_protection"),
    }


# -- evidence -------------------------------------------------------------------


def map_bnb_reason(item: Any) -> dict:
    """One buy/not-buy reason: ``text``, ``prooflink`` citation, ``upvotes``."""
    item = _require_obj(item, "reason")
    text = item.get("text")
    if not isinstance(text, str) or not text:
        raise ToolError(
            "Unexpected Tires API response shape: a reason is missing its 'text'."
        )
    out = {
        "text": _truncate(text, REASON_MAX),
        "prooflink": item.get("prooflink"),
        "upvotes": item.get("upvotes"),
    }
    if _truncated_flag(text, REASON_MAX):
        out["text_truncated"] = True
    return out


def map_material(item: Any) -> dict:
    """A related material — union of the four upstream types.

    ``benchmark`` is the upstream type for comparison results and is NOT
    automatically a professional test; its ``product_rank`` describes this
    model's placement inside that comparison. Links and ``*_truncated``
    markers are the continuation routes for cut text.
    """
    item = _require_obj(item, "material")
    mtype = item.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ToolError(
            "Unexpected Tires API response shape: a material is missing its 'type'."
        )
    out: dict[str, Any] = {
        "type": mtype,
        "title": _clip(item.get("title"), TITLE_MAX),
        "publication_date": item.get("publication_date"),
    }
    if mtype == "article":
        _tag_slice(item.get("tags_list"), key="tags_list", out=out)
        lead = item.get("lead")
        out["lead"] = _truncate(lead, SUMMARY_MAX)
        if _truncated_flag(lead, SUMMARY_MAX):
            out["lead_truncated"] = True
        out["canonical_link"] = item.get("canonical_link")
    elif mtype == "video":
        out["video_url"] = item.get("video_url")
        out["thumbnail"] = item.get("thumbnail")
    elif mtype == "link":
        out["url"] = item.get("url")
        out["website"] = _clip(item.get("website"), DISPLAY_MAX)
        text = item.get("text")
        out["text"] = _truncate(text, SUMMARY_MAX)
        if _truncated_flag(text, SUMMARY_MAX):
            out["text_truncated"] = True
    elif mtype == "benchmark":
        out["season"] = map_named_ref(item.get("season"))
        out["automobile_type"] = map_named_ref(item.get("automobile_type"))
        out["canonical_link"] = item.get("canonical_link")
        rank = item.get("product_rank")
        if rank is None:
            out["product_rank"] = None
        else:
            rank = _require_obj(rank, "product_rank")
            rank_out: dict[str, Any] = {
                "place": rank.get("place"),
                "description": _truncate(rank.get("description"), SUMMARY_MAX),
            }
            _tag_slice(rank.get("positive_tags"), key="positive_tags", out=rank_out)
            _tag_slice(rank.get("negative_tags"), key="negative_tags", out=rank_out)
            out["product_rank"] = rank_out
    else:
        # Unknown upstream-new material type: surface type, title, date and a
        # link when one exists — never invent fields.
        for link_key in ("canonical_link", "url", "video_url"):
            if item.get(link_key):
                out["link"] = item[link_key]
                break
    return out


def map_test_row(item: Any) -> dict:
    """Test list row: identity, year/season/type, tested size, date, link."""
    item = _require_slug(_require_obj(item, "test"), "test")
    out: dict[str, Any] = {
        "slug": item["slug"],
        "title": _clip(item.get("title"), TITLE_MAX),
        "canonical_link": item.get("canonical_link"),
        "year": item.get("year"),
        "season": map_named_ref(item.get("season")),
        "automobile_type": map_named_ref(item.get("automobile_type")),
        "tire_size": _clip(item.get("tire_size"), TEXT_MAX),
        "publication_date": item.get("publication_date"),
    }
    _put_regions(out, item.get("regions"))
    return out


def map_test_header(item: Any) -> dict:
    """Test detail header — kept separately from the paginated participants."""
    return map_test_row(item)


def map_test_participant(item: Any) -> dict:
    """One participant: place, test_score, verdict, tags and model identity.

    ``test_score`` belongs to this test's own scale — never compare it across
    tests. ``product.has_modes`` maps each requested ``sizes`` value to the
    matched designations or ``null``; it annotates availability only. Cut
    ``description`` text continues on the test's ``canonical_link``.
    """
    item = _require_obj(item, "test participant")
    product = _require_slug(
        _require_obj(item.get("product"), "participant product"), "participant product"
    )
    out: dict[str, Any] = {
        "place": item.get("place"),
        "test_score": item.get("test_score"),
        "recommend": item.get("recommend"),
        "description": _truncate(item.get("description"), VERDICT_MAX),
        "product": {
            "slug": product["slug"],
            "display": _clip(product.get("display"), DISPLAY_MAX),
            "brand": _slug_of(product.get("brand")),
            "canonical_link": product.get("canonical_link"),
            "year": product.get("year"),
            "studded": product.get("studded"),
            "for_nordic_winter": product.get("for_nordic_winter"),
            "rating": map_rating(product.get("rating")),
            "has_modes": map_has_modes(product.get("has_modes")),
        },
    }
    if _truncated_flag(item.get("description"), VERDICT_MAX):
        out["description_truncated"] = True
    _tag_slice(item.get("positive_tags"), key="positive_tags", out=out)
    _tag_slice(item.get("negative_tags"), key="negative_tags", out=out)
    _put_regions(out["product"], product.get("regions"))
    return out


# -- reference lists --------------------------------------------------------------


def map_region(item: Any) -> dict:
    """Region reference row: slug, display, tree_level, countries.

    ``countries`` are never count-capped — no per-region detail route exists
    to continue a cut list, so the list is preserved whole (individual names
    are still length-clipped) and the outer ``limit`` plus the response budget
    provide the bound. ``null`` stays ``null``; a present non-list is a
    contract violation.
    """
    item = _require_slug(_require_obj(item, "region"), "region")
    countries = item.get("countries")
    if countries is not None and not isinstance(countries, list):
        raise ToolError(
            "Unexpected Tires API response shape: 'countries' must be a list or "
            f"null, got {type(countries).__name__}."
        )
    return {
        "slug": item["slug"],
        "display": _clip(item.get("display"), DISPLAY_MAX),
        "tree_level": item.get("tree_level"),
        "countries": [_clip(c, COUNTRY_MAX) for c in countries]
        if countries is not None
        else None,
    }


def map_performance_category(item: Any) -> dict:
    """Performance category row: slug, display, season, type, conditions, tags.

    ``description`` and ``tags`` are preserved WHOLE — no per-category detail
    endpoint or citation route exists to continue a cut, so this object is
    bounded only by the outer ``limit`` and the serialized-response budget: an
    extreme single category can exceed the budget even at ``limit=1`` and then
    honestly fails instead of silently dropping its text. ``season`` keeps
    ``null`` slug/display for season-agnostic categories.
    """
    item = _require_slug(_require_obj(item, "performance category"), "performance category")
    out: dict[str, Any] = {
        "slug": item["slug"],
        "display": _clip(item.get("display"), DISPLAY_MAX),
        "season": map_named_ref(item.get("season")),
        "automobile_type": map_named_ref(item.get("automobile_type")),
        "road_conditions": _clip(item.get("road_conditions"), DISPLAY_MAX),
        "description": item.get("description"),
    }
    tags = item.get("tags")
    if tags is not None and not isinstance(tags, list):
        raise ToolError(
            "Unexpected Tires API response shape: performance-category 'tags' "
            f"must be a list or null, got {type(tags).__name__}."
        )
    out["tags"] = [_clip(t, TITLE_MAX) if isinstance(t, str) else t for t in tags] \
        if tags is not None else None
    return out
