"""Domain tool behavior — all 12 tires_* tools through the registered MCP
interface, against injectable ``httpx.MockTransport`` (no respx, no network).

Fixtures under ``tests/fixtures/`` are compact sanitized copies of the
live-recorded shapes documented in ``docs/live-validation.md``.
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
from tiresvote_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Router:
    """Path → payload router for ``httpx.MockTransport``; records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Any] = {}

    def add(self, path: str, payload: Any) -> "Router":
        self.routes[path] = payload
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"detail": "unmocked path"})
        if callable(body):
            return body(request)
        return httpx.Response(200, json=body)

    def params(self) -> httpx.QueryParams:
        assert self.requests, "no upstream request was made"
        return httpx.QueryParams(self.requests[-1].url.query)


def _server(router: Router):
    client = TiresClient(
        TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET),
        transport=httpx.MockTransport(router.handler),
    )
    return create_server(client=client)


async def _call(server, name: str, arguments: dict | None = None) -> Any:
    result = await server.call_tool(name, arguments or {})
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _paged_meta(next_page: int | None, *, path: str, total: int = 17) -> dict:
    """meta.pagination whose 'next' link stays on the test host."""
    next_url = f"{BASE_URL}{path}?per_page=3&page={next_page}" if next_page else None
    return {
        "pagination": {
            "current_page_count": 3,
            "total_items": total,
            "total_pages": 6,
            "first": f"{BASE_URL}{path}?per_page=3&page=1",
            "prev": None,
            "next": next_url,
            "last": f"{BASE_URL}{path}?per_page=3&page=6",
        }
    }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


async def test_list_brand_tires_maps_filters_and_slices():
    router = Router().add("/v2/tires/catalog/michelin/", _fixture("catalog_brand.json"))
    server = _server(router)

    env = await _call(
        server,
        "tires_list_brand_tires",
        {
            "brand": "michelin",
            "regions": ["eudm", "usdm"],
            "seasons": ["summer", "winter"],
            "automobile_type": "car",
            "runflat": True,
            "include_discontinued": True,
            "include_oe": False,
            "ordering": "-popularity",
            "limit": 2,
        },
    )

    params = router.params()
    assert params.get_list("region") == ["eudm", "usdm"]
    assert params.get_list("season") == ["summer", "winter"]
    assert params.get("automobile_type") == "car"
    assert params.get("runflat") == "true"
    assert params.get("show_discontinued") == "true"
    assert params.get("show_oe") == "false"  # explicit False is preserved, not dropped
    assert params.get("ordering") == "-popularity"
    # MCP paging is local — never sent upstream
    assert "limit" not in params and "offset" not in params

    assert env["total"] == 3 and env["available_count"] == 3
    assert len(env["results"]) == 2 and env["has_more"] is True and env["next_offset"] == 2
    row = env["results"][0]
    assert row["slug"] == "pilot-sport-4s"
    assert row["brand"] == "michelin"  # reduced to slug
    assert row["season"] == "summer"
    assert row["regions"] == ["north-america", "ladm", "eudm", "russia", "jdm",
                              "skdm", "sam", "medm", "africa", "audm"]
    assert row["canonical_link"].startswith("https://tiresvote.com/")
    assert TEST_SECRET not in json.dumps(env)


async def test_list_brand_tires_empty_filter_rejected_before_request():
    router = Router().add("/v2/tires/catalog/michelin/", _fixture("catalog_brand.json"))
    server = _server(router)

    with pytest.raises(ToolError, match="regions"):
        await _call(server, "tires_list_brand_tires", {"brand": "michelin", "regions": []})
    assert router.requests == []


async def test_list_brand_tires_truncated_cap_reported():
    router = Router().add(
        "/v2/tires/catalog/bridgestone/", _fixture("catalog_brand_truncated.json")
    )
    server = _server(router)

    env = await _call(server, "tires_list_brand_tires", {"brand": "bridgestone"})

    assert env["available_count"] == 3
    assert env["total"] == 318  # upstream count exceeds the fetched set
    assert env["truncated"] is True
    assert "tires_search_advanced" in env["hint"]


async def test_list_brand_tires_unknown_brand_is_404():
    router = Router()  # no route registered
    server = _server(router)

    with pytest.raises(ToolError, match="404"):
        await _call(server, "tires_list_brand_tires", {"brand": "nosuchbrand"})


async def test_slug_injection_rejected_on_path_params():
    router = Router().add("/v2/tires/catalog/michelin/", _fixture("catalog_brand.json"))
    server = _server(router)

    for bad in ("../evil", "michelin/x", "a%2Fb", "a?b=1", "a b"):
        with pytest.raises(ToolError):
            await _call(server, "tires_get_tire", {"brand": bad, "product": "x"})
    assert router.requests == []


async def test_get_tire_concise_omits_long_fields():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/", _fixture("tire_detail.json")
    )
    server = _server(router)

    card = await _call(
        server, "tires_get_tire", {"brand": "michelin", "product": "pilot-sport-4"}
    )

    assert card["slug"] == "pilot-sport-4"
    assert card["brand"]["slug"] == "michelin"
    assert card["season"]["slug"] == "summer"
    assert card["rating"]["score"] is not None
    assert "popularity" in card["rating"]
    assert card["last_update"]  # meta.last_update preserved
    assert "counters" in card
    # concise keeps the card compact — no description/tags/image
    assert "description" not in card and "tags" not in card and "image" not in card


async def test_get_tire_full_includes_bounded_description():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/", _fixture("tire_detail.json")
    )
    server = _server(router)

    card = await _call(
        server,
        "tires_get_tire",
        {"brand": "michelin", "product": "pilot-sport-4", "detail": "full"},
    )

    assert isinstance(card["description"], str) and card["description"]
    assert len(card["description"]) <= 4100  # bounded, flag set when cut
    assert "tags" in card and "image" in card
    assert card["canonical_link"]


async def test_get_tire_preserves_family_links_and_oe_flags():
    router = Router()
    router.add(
        "/v2/tires/catalog/michelin/primacy-3/", _fixture("tire_detail_family.json")
    )
    router.add(
        "/v2/tires/catalog/michelin/energy-lx4/", _fixture("tire_detail_oe.json")
    )
    server = _server(router)

    fam = await _call(
        server, "tires_get_tire", {"brand": "michelin", "product": "primacy-3"}
    )
    assert fam["ancestor"] == {
        "brand": "michelin",
        "product": "primacy-hp",
        "display": "Primacy HP",
    }
    assert fam["runflat_models"] == [
        {"brand": "michelin", "product": "primacy-3-zp", "display": "Primacy 3 ZP"}
    ]
    assert fam["successors"] == []  # empty list stays empty, not null

    oe = await _call(
        server, "tires_get_tire", {"brand": "michelin", "product": "energy-lx4"}
    )
    assert oe["is_oe_model"] is True
    assert oe["discontinued"] is True


async def test_list_sizes_preserves_indices_and_decimal_geometry():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/modes/", _fixture("modes.json")
    )
    server = _server(router)

    env = await _call(
        server, "tires_list_sizes", {"brand": "michelin", "product": "pilot-sport-4"}
    )

    modes = env["results"]
    assert env["total"] == 4
    lt = next(m for m in modes if m["sizing_system"] == "lt-metric")
    assert lt["dual_load_index"] is not None  # LT rows can carry dual index
    flot = next(m for m in modes if m["sizing_system"] == "flotation")
    assert flot["rim_diameter"] == 17.0 and isinstance(flot["rim_diameter"], float)
    assert flot["overall_diameter"] == 35.0 and flot["section_width"] == 12.5
    metric = modes[0]
    assert metric["text"].startswith("2")  # e.g. '225/45 R17 94Y XL'
    assert "load_index" in metric and "speed_index" in metric


async def test_list_regions_keeps_countries_and_tree_level():
    router = Router().add("/v2/tires/regions/", _fixture("regions.json"))
    server = _server(router)

    env = await _call(server, "tires_list_regions", {"limit": 2})

    assert env["total"] == 20 and env["has_more"] is True and env["next_offset"] == 2
    for region in env["results"]:
        assert region["slug"] and "tree_level" in region
        assert isinstance(region["countries"], list)  # preserved whole


async def test_list_performance_categories_nullable_season():
    router = Router().add(
        "/v2/tires/performance-categories/", _fixture("perf_categories.json")
    )
    server = _server(router)

    env = await _call(server, "tires_list_performance_categories")

    cat = env["results"][0]
    assert cat["slug"] == "suv-on-off-road-commercial-traction"
    # season-agnostic category: null slug/display preserved, not dropped
    assert cat["season"] == {"slug": None, "display": None}
    assert cat["automobile_type"]["slug"] == "suv"
    assert isinstance(cat["tags"], list) and cat["description"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_sends_query_and_paginates_api_pages():
    payload = _fixture("search.json")
    payload["meta"] = _paged_meta(2, path="/v2/tires/search/")
    router = Router().add("/v2/tires/search/", payload)
    server = _server(router)

    env = await _call(server, "tires_search", {"query": "pilot sport", "per_page": 3})

    params = router.params()
    assert params.get("query") == "pilot sport"  # upstream param is 'query'
    assert params.get("per_page") == "3" and params.get("page") == "1"

    assert env["has_more"] is True and env["next_page"] == 2
    assert env["pagination_limited"] is False
    row = env["results"][0]
    assert row["slug"] and row["canonical_link"]
    assert row["has_modes"] == {}  # no sizes requested → empty map
    assert "score" in row["rating"] and "popularity" in row["rating"]


async def test_search_next_link_off_path_is_limited_not_followed():
    payload = _fixture("search.json")
    payload["meta"] = {
        "pagination": {
            "current_page_count": 3,
            "next": "https://tiresvote.com/search/?query=pilot+sport",
            "total_items": 100,
        }
    }
    router = Router().add("/v2/tires/search/", payload)
    server = _server(router)

    env = await _call(server, "tires_search", {"query": "pilot sport"})

    assert env["has_more"] is False
    assert env["pagination_limited"] is True
    assert env["site_url"].startswith("https://tiresvote.com/")
    assert TEST_SECRET not in env["site_url"]


async def test_search_advanced_maps_all_filters_to_repeated_params():
    router = Router().add("/v2/tires/search/advanced/", _fixture("search_advanced.json"))
    server = _server(router)

    await _call(
        server,
        "tires_search_advanced",
        {
            "brands": ["michelin", "pirelli"],
            "regions": ["eudm"],
            "seasons": ["summer", "winter"],
            "automobile_types": ["car"],
            "performance_categories": ["car-max-performance-summer"],
            "price_segments": ["premium"],
            "production_years": [2020, 2021],
            "tire_widths": [205, 225],
            "aspect_ratios": [45],
            "rim_diameters": [17],
            "speed_indices": ["V", "W"],
            "load_indices": [91, 94],
            "sizes": ["225/45R17", "315/35R20"],
            "include_discontinued": True,
            "runflat_filter": False,
            "include_oe": True,
            "extra_load": True,
            "mud_and_snow": False,
            "nordic_winter": True,
            "ordering": "score,-popularity",
            "page": 2,
            "per_page": 5,
        },
    )

    params = router.params()
    assert params.get_list("b") == ["michelin", "pirelli"]
    assert params.get_list("reg") == ["eudm"]
    assert params.get_list("s") == ["summer", "winter"]
    assert params.get_list("at") == ["car"]
    assert params.get_list("pc") == ["car-max-performance-summer"]
    assert params.get_list("ps") == ["premium"]
    assert params.get_list("y") == ["2020", "2021"]
    assert params.get_list("tw") == ["205", "225"]
    assert params.get_list("ar") == ["45"]
    assert params.get_list("rd") == ["17"]
    assert params.get_list("si") == ["V", "W"]
    assert params.get_list("li") == ["91", "94"]
    assert params.get_list("t") == ["225/45R17", "315/35R20"]
    assert params.get("np") == "true"
    assert params.get("rf") == "false"  # neutral flag: explicit false preserved
    assert params.get("oe") == "true"
    assert params.get("xl") == "true"
    assert params.get("ms") == "false"
    assert params.get("nw") == "true"
    assert params.get("ordering") == "score,-popularity"
    assert params.get("page") == "2" and params.get("per_page") == "5"


async def test_search_advanced_omits_unset_flags():
    router = Router().add("/v2/tires/search/advanced/", _fixture("search_advanced.json"))
    server = _server(router)

    await _call(server, "tires_search_advanced", {"brands": ["michelin"]})

    params = router.params()
    for flag in ("np", "rf", "oe", "xl", "ms", "nw"):
        assert flag not in params  # omitted ≠ explicit false


async def test_search_advanced_has_modes_list_and_null_preserved():
    router = Router().add("/v2/tires/search/advanced/", _fixture("search_advanced.json"))
    server = _server(router)

    env = await _call(
        server,
        "tires_search_advanced",
        {"brands": ["michelin"], "sizes": ["225/45R17", "315/35R20"]},
    )

    primacy = next(r for r in env["results"] if r["slug"] == "primacy-4plus")
    assert primacy["has_modes"]["225/45R17"]  # matched designations list
    assert primacy["has_modes"]["315/35R20"] is None  # null preserved verbatim
    ps4s = next(r for r in env["results"] if r["slug"] == "pilot-sport-4s")
    assert ps4s["has_modes"]["225/45R17"] == ["225/45 R17 94Y XL"]


async def test_search_row_without_slug_is_contract_error():
    payload = _fixture("search.json")
    payload["data"] = [{"display": "No identity here"}]  # malformed 200 row
    router = Router().add("/v2/tires/search/", payload)
    server = _server(router)

    with pytest.raises(ToolError, match="slug"):
        await _call(server, "tires_search", {"query": "x"})


async def test_search_has_modes_bool_is_contract_error():
    payload = _fixture("search.json")
    row = dict(payload["data"][0])
    row["has_modes"] = {"225/45R17": True}  # the Swagger snapshot's wrong type
    payload["data"] = [row]
    router = Router().add("/v2/tires/search/", payload)
    server = _server(router)

    with pytest.raises(ToolError, match="has_modes"):
        await _call(server, "tires_search", {"query": "x"})


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


async def test_pros_cons_independent_offsets():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/bnb/", _fixture("bnb.json")
    )
    server = _server(router)

    env = await _call(
        server,
        "tires_get_pros_cons",
        {"brand": "michelin", "product": "pilot-sport-4", "limit": 2, "buy_offset": 2},
    )

    buy, not_buy = env["buy"], env["not_buy"]
    assert buy["total"] == 4 and not_buy["total"] == 1
    assert len(buy["reasons"]) == 2  # reasons 3..4 after buy_offset=2
    assert buy["offset"] == 2 and buy["has_more"] is False
    assert not_buy["offset"] == 0 and not_buy["has_more"] is False
    reason = buy["reasons"][0]
    assert reason["text"] and reason["prooflink"].startswith("https://tiresvote.com/")
    assert isinstance(reason["upvotes"], int)

    # advancing the other side only touches its own slice
    env2 = await _call(
        server,
        "tires_get_pros_cons",
        {"brand": "michelin", "product": "pilot-sport-4", "limit": 1, "not_buy_offset": 1},
    )
    assert env2["not_buy"]["offset"] == 1 and env2["not_buy"]["reasons"] == []
    assert env2["buy"]["offset"] == 0


async def test_pros_cons_null_data_is_distinct_from_empty_lists():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-3-zp2/bnb/", _fixture("bnb_null.json")
    )
    router.add(
        "/v2/tires/catalog/michelin/pilot-sport-4/bnb/",
        {"data": {"buy": [], "not_buy": []}, "meta": {}},
    )
    server = _server(router)

    null_env = await _call(
        server,
        "tires_get_pros_cons",
        {"brand": "michelin", "product": "pilot-sport-3-zp2"},
    )
    assert null_env["buy"] is None and null_env["not_buy"] is None
    assert "no approved pros/cons" in null_env["note"]

    empty_env = await _call(
        server, "tires_get_pros_cons", {"brand": "michelin", "product": "pilot-sport-4"}
    )
    assert empty_env["buy"]["total"] == 0 and empty_env["buy"]["reasons"] == []
    assert empty_env["buy"] is not None  # empty ≠ absent


async def test_pros_cons_missing_side_is_contract_error():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/bnb/",
        {"data": {"buy": [{"text": "ok", "prooflink": "x", "upvotes": 1}]}, "meta": {}},
    )
    server = _server(router)

    with pytest.raises(ToolError, match="not_buy"):
        await _call(
            server, "tires_get_pros_cons", {"brand": "michelin", "product": "pilot-sport-4"}
        )


async def test_list_materials_covers_all_four_types():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/materials/", _fixture("materials.json")
    )
    server = _server(router)

    env = await _call(
        server, "tires_list_materials", {"brand": "michelin", "product": "pilot-sport-4"}
    )

    by_type = {m["type"]: m for m in env["results"]}
    assert set(by_type) == {"article", "video", "benchmark", "link"}
    assert by_type["article"]["canonical_link"].startswith("https://tiresvote.com/")
    assert "lead" in by_type["article"]
    assert by_type["video"]["video_url"]
    assert by_type["link"]["url"].startswith("https://example.test/")
    # benchmark keeps its type — not relabeled a professional test
    assert by_type["benchmark"]["type"] == "benchmark"
    assert "product_rank" in by_type["benchmark"]
    assert by_type["article"]["publication_date"]


async def test_list_materials_type_filter_param():
    router = Router().add(
        "/v2/tires/catalog/michelin/pilot-sport-4/materials/", _fixture("materials.json")
    )
    server = _server(router)

    await _call(
        server,
        "tires_list_materials",
        {"brand": "michelin", "product": "pilot-sport-4", "material_type": "video"},
    )
    assert router.params().get("type") == "video"

    with pytest.raises(Exception):
        await _call(
            server,
            "tires_list_materials",
            {"brand": "michelin", "product": "pilot-sport-4", "material_type": "pdf"},
        )


async def test_list_tests_api_paging_and_filters():
    payload = _fixture("tests_list.json")
    payload["meta"] = _paged_meta(2, path="/v2/tires/tests/")
    router = Router().add("/v2/tires/tests/", payload)
    server = _server(router)

    env = await _call(
        server,
        "tires_list_tests",
        {"years": [2026, 2027], "seasons": ["winter"], "automobile_type": "car"},
    )

    params = router.params()
    assert params.get_list("year") == ["2026", "2027"]
    assert params.get_list("season") == ["winter"]
    assert params.get("automobile_type") == "car"
    assert params.get("page") == "1" and params.get("per_page") == "10"

    assert env["has_more"] is True and env["next_page"] == 2
    row = env["results"][0]
    assert row["slug"] and row["canonical_link"].startswith("https://tiresvote.com/")
    assert "tire_size" in row and "test_score" not in row  # rows have no scores


async def test_get_test_slices_participants_independently_of_header():
    router = Router().add(
        "/v2/tires/tests/2027-adac-winter-tire-test-r17/", _fixture("test_detail.json")
    )
    server = _server(router)

    env = await _call(
        server,
        "tires_get_test",
        {"slug": "2027-adac-winter-tire-test-r17", "sizes": ["225/50R17"], "limit": 2},
    )

    assert router.params().get_list("has_mode") == ["225/50R17"]
    header = env["test"]
    assert header["slug"] == "2027-adac-winter-tire-test-r17"
    assert header["tire_size"] and header["canonical_link"]
    assert env["sizes_checked"] == ["225/50R17"]

    parts = env["participants"]
    assert parts["total"] == 3 and len(parts["results"]) == 2
    assert parts["has_more"] is True and parts["next_offset"] == 2
    winner = parts["results"][0]
    assert winner["place"] == 1
    assert winner["test_score"] == 2.2  # test scale, distinct from rating.score
    assert winner["recommend"] is True
    assert winner["positive_tags"] == ["Aquaplaning", "Wet Braking"]
    assert winner["product"]["slug"] == "cinturato-winter-3"
    assert winner["product"]["has_modes"]["225/50R17"] == ["225/50 R17 98V M+S XL"]
    assert winner["product"]["rating"]["score"] is not None


async def test_get_test_has_modes_null_preserved():
    router = Router().add(
        "/v2/tires/tests/2025-adac-summer-tire-test-r15/",
        _fixture("test_detail_othermode.json"),
    )
    server = _server(router)

    env = await _call(
        server,
        "tires_get_test",
        {"slug": "2025-adac-summer-tire-test-r15", "sizes": ["195/65R15"]},
    )
    by_slug = {p["product"]["slug"]: p for p in env["participants"]["results"]}
    assert by_slug["cinturato-winter-3"]["product"]["has_modes"]["195/65R15"] is None
    assert by_slug["wintercontact-ts-870"]["product"]["has_modes"]["195/65R15"]


async def test_get_test_missing_items_is_contract_error():
    router = Router().add(
        "/v2/tires/tests/x/",
        {"data": {"slug": "x", "title": "t"}, "meta": {}},
    )
    server = _server(router)

    with pytest.raises(ToolError, match="items"):
        await _call(server, "tires_get_test", {"slug": "x"})


async def test_get_test_size_over_30_chars_rejected():
    router = Router().add("/v2/tires/tests/x/", _fixture("test_detail.json"))
    server = _server(router)

    with pytest.raises(Exception):
        await _call(
            server, "tires_get_test", {"slug": "x", "sizes": ["9" * 31]}
        )
    assert router.requests == []


async def test_get_test_participant_without_product_slug_rejected():
    payload = _fixture("test_detail.json")
    bad = dict(payload["data"]["items"][0])
    bad["product"] = {"display": "no slug"}  # malformed identity
    payload["data"]["items"] = [bad]
    router = Router().add(
        "/v2/tires/tests/2027-adac-winter-tire-test-r17/", payload
    )
    server = _server(router)

    with pytest.raises(ToolError, match="slug"):
        await _call(
            server, "tires_get_test", {"slug": "2027-adac-winter-tire-test-r17"}
        )
