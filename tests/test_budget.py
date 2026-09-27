"""Response-budget tests: every tool must stay inside ~40 KB serialized JSON
(roughly 8–10k tokens — a target, not an exact count) OR fail with a compact,
actionable ToolError telling the caller to reduce limit/per_page/filters.

Each case feeds a deliberately pathological upstream payload through a
registered tool call (MockTransport, no network) and asserts one of the two
honest outcomes — never silent identity loss and never unbounded output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from tests.conftest import BASE_URL, TEST_SECRET
from tiresvote_mcp.client import TiresClient, TiresSettings
from tiresvote_mcp.projections import MAX_RESPONSE_BYTES
from tiresvote_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _server(payload: Any, path: str = "/v2/tires/catalog/"):
    """One-route server; every path not in `path` answers 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == path:
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"detail": "unmocked"})

    client = TiresClient(
        TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET),
        transport=httpx.MockTransport(handler),
    )
    return create_server(client=client)


async def _bounded_call(server, name: str, args: dict) -> dict:
    """Assert the tool either returns within budget or raises the budget error."""
    try:
        result = await server.call_tool(name, args)
        payload = (
            result.structured_content
            if result.structured_content is not None
            else json.loads(result.content[0].text)
        )
    except ToolError as exc:
        message = str(exc)
        # The only acceptable failure here is the compact budget guard itself —
        # actionable (tells the caller what to reduce) and secret-free.
        assert "budget" in message
        assert "Reduce limit/per_page or narrow the filters" in message
        assert TEST_SECRET not in message
        return {}
    size = len(json.dumps(payload, ensure_ascii=False).encode())
    assert size <= MAX_RESPONSE_BYTES, f"{name} returned {size} bytes unchecked"
    return payload


def _fat_product_row(slug: str) -> dict:
    return {
        "slug": slug,
        "display": slug + " " + "X" * 300,
        "brand": {"slug": "michelin", "display": "M" * 400},
        "canonical_link": f"https://tiresvote.com/catalog/michelin/{slug}/",
        "season": {"slug": "summer", "display": "Summer"},
        "automobile_type": {"slug": "car", "display": "Passenger"},
        "year": 2020,
        "discontinued": False,
        "almost_discontinued": False,
        "coming_soon": False,
        "is_runflat": False,
        "counters": {"modes": 12},
        "regions": [
            {"slug": f"r{i}", "display": "R" * 200} for i in range(40)
        ],
        "rating": {
            "score": 90,
            "popularity": 10,
            "tags": [
                {"slug": f"t{i}", "display": "T" * 200, "connotation": "good"}
                for i in range(30)
            ],
        },
        "has_modes": {
            f"{205 + i}/45R17": [f"{205 + i}/45 R17 9{i}V " + "M" * 90 for _ in range(20)]
            for i in range(10)
        },
    }


# ---------------------------------------------------------------------------
# Worst-case calls per tool
# ---------------------------------------------------------------------------


async def test_brands_pathological_rows_bounded_or_error():
    rows = [
        {
            "slug": f"brand-{i}",
            "display": "B" * 3000,
            "price_segment": "Premium",
            "products_count": 999,
        }
        for i in range(50)
    ]
    server = _server({"data": rows, "meta": {"count": 50}})
    env = await _bounded_call(server, "tires_list_brands", {"limit": 50})
    if env:
        # bounded by design when it succeeds
        assert len(env["results"]) == 50


async def test_brand_tires_pathological_rows_bounded_or_error():
    server = _server(
        {"data": [_fat_product_row(f"m{i}") for i in range(50)], "meta": {"count": 50}},
        "/v2/tires/catalog/michelin/",
    )
    env = await _bounded_call(
        server, "tires_list_brand_tires", {"brand": "michelin", "limit": 50}
    )
    if env:
        row = env["results"][0]
        # catalog rows carry no rating; the regions cap + marker applied
        assert row["regions_more"] == 30 and len(row["regions"]) == 10


async def test_get_tire_full_card_bounded_or_concise_recovery():
    card = _fat_product_row("big-card")
    card.update(
        {
            "description": "D" * 9000,
            "tags": ["t" * 500 for _ in range(40)],
            "successors": [
                {"brand_slug": "b", "product_slug": f"s{i}", "display_name": "S" * 90}
                for i in range(40)
            ],
            "runflat_models": [],
            "image": "https://ws-tires.s3.amazonaws.com/x.png",
        }
    )
    server = _server(
        {"data": card, "meta": {"last_update": "2024-01-01"}},
        "/v2/tires/catalog/michelin/big-card/",
    )
    env = await _bounded_call(
        server, "tires_get_tire", {"brand": "michelin", "product": "big-card", "detail": "full"}
    )
    if env:
        assert env["description_truncated"] is True
        assert len(env["description"]) <= 4000 + 40
        assert env["tags_more"] == 28
        assert env["successors_more"] == 30


async def test_list_sizes_many_variants_bounded():
    modes = [
        {
            "sizing_system": "metric",
            "text": f"{200 + i}/45 R{15 + (i % 8)} 9{i % 10}V XL",
            "tire_width": 200 + i,
            "aspect_ratio": 45,
            "rim_diameter": float(15 + (i % 8)),
            "overall_diameter": None,
            "section_width": None,
            "load_index": 90 + i % 10,
            "dual_load_index": None,
            "speed_index": "V",
            "extra_load": True,
            "mud_and_snow": False,
            "rim_protection": True,
        }
        for i in range(50)
    ]
    server = _server(
        {"data": modes, "meta": {"count": 50}},
        "/v2/tires/catalog/michelin/x/modes/",
    )
    env = await _bounded_call(
        server, "tires_list_sizes", {"brand": "michelin", "product": "x", "limit": 50}
    )
    assert env and len(env["results"]) == 50  # always fits — flat scalar rows


async def test_regions_huge_countries_bounded_or_error():
    rows = [
        {
            "slug": f"region-{i}",
            "display": f"Region {i}",
            "tree_level": 1,
            "countries": ["Country " + "C" * 60 for _ in range(120)],
        }
        for i in range(5)
    ]
    server = _server({"data": rows, "meta": {"count": 5}}, "/v2/tires/regions/")
    env = await _bounded_call(server, "tires_list_regions", {"limit": 5})
    if env:
        # countries are never count-capped — all 120 survive or none do
        assert len(env["results"][0]["countries"]) == 120


async def test_performance_category_single_row_can_honestly_overflow():
    rows = [
        {
            "slug": "fat-cat",
            "display": "Huge category",
            "season": {"slug": None, "display": None},
            "automobile_type": {"slug": "car", "display": "Passenger"},
            "road_conditions": "All",
            "tags": ["t" * 800 for _ in range(60)],
            "description": "D" * 50000,
        }
    ]
    server = _server(
        {"data": rows, "meta": {"count": 1}}, "/v2/tires/performance-categories/"
    )
    # An extreme single row must error honestly even at limit=1 — never cut.
    with pytest.raises(ToolError, match="budget"):
        await server.call_tool("tires_list_performance_categories", {"limit": 1})


async def test_search_page_of_fat_rows_bounded_or_error():
    payload = {
        "data": [_fat_product_row(f"s{i}") for i in range(20)],
        "meta": {"pagination": {"current_page_count": 20, "next": None}},
    }
    server = _server(payload, "/v2/tires/search/")
    env = await _bounded_call(
        server, "tires_search", {"query": "x", "per_page": 20}
    )
    if env:
        row = env["results"][0]
        assert row["has_modes"] is not None


async def test_search_advanced_page_of_fat_rows_bounded_or_error():
    payload = {
        "data": [_fat_product_row(f"s{i}") for i in range(20)],
        "meta": {"pagination": {"current_page_count": 20, "next": None}},
    }
    server = _server(payload, "/v2/tires/search/advanced/")
    env = await _bounded_call(
        server, "tires_search_advanced", {"per_page": 20, "sizes": ["225/45R17"]}
    )
    if env:
        row = env["results"][0]
        hm = row["has_modes"]
        if hm:
            # per-size lists capped at 8 designations
            assert all(v is None or len(v) <= 8 for v in hm.values())


async def test_pros_cons_two_heavy_sides_bounded_or_error():
    reasons = [
        {"text": "R" * 900, "prooflink": "https://tiresvote.com/x", "upvotes": i}
        for i in range(100)
    ]
    server = _server(
        {"data": {"buy": reasons, "not_buy": reasons}, "meta": {}},
        "/v2/tires/catalog/michelin/x/bnb/",
    )
    env = await _bounded_call(
        server,
        "tires_get_pros_cons",
        {"brand": "michelin", "product": "x", "limit": 50},
    )
    if env:
        assert all(len(r["text"]) <= 450 for r in env["buy"]["reasons"])
        assert env["buy"]["reasons"][0]["text_truncated"] is True


async def test_materials_heavy_rows_bounded_or_error():
    mats = [
        {
            "type": "article",
            "title": "T" * 500,
            "publication_date": "2024-01-01",
            "tags_list": ["x" * 100 for _ in range(30)],
            "lead": "L" * 3000,
            "canonical_link": "https://tiresvote.com/a",
        }
        for _ in range(50)
    ]
    server = _server(
        {"data": mats, "meta": {"count": 50}},
        "/v2/tires/catalog/michelin/x/materials/",
    )
    env = await _bounded_call(
        server, "tires_list_materials", {"brand": "michelin", "product": "x", "limit": 50}
    )
    if env:
        m = env["results"][0]
        assert m["lead_truncated"] is True and len(m["lead"]) <= 450
        assert m["tags_list_more"] == 18
        assert len(m["title"]) <= 210


async def test_list_tests_page_bounded_or_error():
    rows = [
        {
            "slug": f"test-{i}",
            "title": "T" * 900,
            "canonical_link": f"https://tiresvote.com/t/{i}",
            "year": 2026,
            "season": {"slug": "winter", "display": "W" * 300},
            "automobile_type": {"slug": "car", "display": "C"},
            "tire_size": "225/50 R17",
            "publication_date": "2026-03-01",
            "regions": [{"slug": "eudm", "display": "Europe"}],
        }
        for i in range(20)
    ]
    payload = {"data": rows, "meta": {"pagination": {"current_page_count": 20, "next": None}}}
    server = _server(payload, "/v2/tires/tests/")
    env = await _bounded_call(server, "tires_list_tests", {"per_page": 20})
    if env:
        assert all(len(r["title"]) <= 210 for r in env["results"])


async def test_get_test_heavy_participants_bounded_or_error():
    participants = [
        {
            "place": i,
            "test_score": 5.5,
            "recommend": True,
            "description": "V" * 2000,
            "positive_tags": ["p" * 300 for _ in range(30)],
            "negative_tags": ["n" * 300 for _ in range(30)],
            "product": _fat_product_row(f"prod-{i}"),
        }
        for i in range(50)
    ]
    payload = {
        "data": {
            "slug": "big-test",
            "title": "Big test",
            "canonical_link": "https://tiresvote.com/t/big",
            "year": 2026,
            "season": {"slug": "winter", "display": "Winter"},
            "automobile_type": {"slug": "car", "display": "Car"},
            "tire_size": "225/50 R17",
            "publication_date": "2026-03-01",
            "regions": [],
            "items": participants,
        },
        "meta": {},
    }
    server = _server(payload, "/v2/tires/tests/big-test/")
    env = await _bounded_call(
        server, "tires_get_test", {"slug": "big-test", "limit": 50}
    )
    if env:
        p = env["participants"]["results"][0]
        assert p["description_truncated"] is True
        assert len(p["description"]) <= 650
        assert p["positive_tags_more"] == 18


# ---------------------------------------------------------------------------
# Guard unit behavior
# ---------------------------------------------------------------------------


def test_budget_guard_passes_normal_and_rejects_oversize():
    from tiresvote_mcp.projections import ensure_within_budget

    assert ensure_within_budget({"ok": True}) == {"ok": True}
    with pytest.raises(ToolError, match="Reduce limit/per_page"):
        ensure_within_budget({"blob": "x" * (MAX_RESPONSE_BYTES + 100)})
