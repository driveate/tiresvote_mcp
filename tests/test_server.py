"""Tests through the registered MCP interface: tools/list, schema, resources,
and tires_list_brands end-to-end against mocked HTTP."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from tests.conftest import BASE_URL, TEST_SECRET
from tiresvote_mcp.client import TiresClient, TiresSettings
from tiresvote_mcp.server import create_server

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = f"{BASE_URL}/v2/tires/catalog/"

EXPECTED_TOOLS = {
    "tires_list_brands",
    "tires_list_brand_tires",
    "tires_get_tire",
    "tires_list_sizes",
    "tires_search",
    "tires_search_advanced",
    "tires_get_pros_cons",
    "tires_list_materials",
    "tires_list_tests",
    "tires_get_test",
    "tires_list_regions",
    "tires_list_performance_categories",
}


def _brands_payload():
    return json.loads((FIXTURES / "brands.json").read_text())


# ---------------------------------------------------------------------------
# Discovery and metadata
# ---------------------------------------------------------------------------


async def test_tools_list_exposes_exactly_the_inventory(mcp_server):
    tools = await mcp_server.list_tools()
    names = [t.name for t in tools]
    assert len(names) == 12 and set(names) == EXPECTED_TOOLS
    for tool in tools:
        assert tool.annotations and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description
        assert tool.tags  # every tool carries a scope tag


async def test_tool_schema_has_bounded_pagination_and_array_filter(mcp_server):
    tools = await mcp_server.list_tools()
    tool = next(t for t in tools if t.name == "tires_list_brands")
    schema = tool.parameters  # JSON Schema for the arguments object
    props = schema["properties"]
    assert set(props) == {"price_segments", "limit", "offset"}
    assert '"array"' in json.dumps(props["price_segments"]) or props[
        "price_segments"
    ].get("type") == "array"
    limit_schema = props["limit"]
    assert limit_schema.get("minimum") == 1 and limit_schema.get("maximum") == 50


async def test_config_status_is_safe(mcp_server):
    result = await mcp_server.read_resource("config://status")
    payload = json.loads(result.contents[0].content)

    assert payload["server"] == "tiresvote"
    assert payload["api_key_configured"] is True
    assert payload["api_base_url"] == BASE_URL
    assert isinstance(payload["version"], str) and payload["version"]
    assert TEST_SECRET not in json.dumps(payload)


async def test_config_status_without_key(mcp_server):
    bare = create_server(client=TiresClient(TiresSettings(base_url=BASE_URL)))
    result = await bare.read_resource("config://status")
    payload = json.loads(result.contents[0].content)
    assert payload["api_key_configured"] is False


# ---------------------------------------------------------------------------
# tires_list_brands via mcp.call_tool
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_brands_happy_path(call_tool):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json=_brands_payload()))

    env = await call_tool("tires_list_brands")

    assert [b["slug"] for b in env["results"]] == [
        "michelin",
        "kenda",
        "nankang",
        "testbrand-unsegmented",
    ]
    assert env["results"][3]["price_segment"] is None  # null preserved, not dropped
    assert env["total"] == 4 and env["available_count"] == 4
    assert env["has_more"] is False and env["truncated"] is False
    # key actually reached upstream
    params = httpx.QueryParams(route.calls.last.request.url.query)
    assert params.get("user_key") == TEST_SECRET


@respx.mock
async def test_list_brands_price_segments_become_repeated_params(call_tool):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json=_brands_payload()))

    await call_tool("tires_list_brands", {"price_segments": ["premium", "economy"]})

    params = httpx.QueryParams(route.calls.last.request.url.query)
    assert params.get_list("price_segment") == ["premium", "economy"]


@respx.mock
async def test_list_brands_limit_offset_slice(call_tool):
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json=_brands_payload()))

    env = await call_tool("tires_list_brands", {"limit": 2, "offset": 1})

    assert [b["slug"] for b in env["results"]] == ["kenda", "nankang"]
    assert env["has_more"] is True and env["next_offset"] == 3


@respx.mock
async def test_list_brands_empty_segment_list_rejected_without_request(call_tool):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json=_brands_payload()))

    with pytest.raises(ToolError, match="price_segments"):
        await call_tool("tires_list_brands", {"price_segments": []})
    assert route.call_count == 0


@respx.mock
async def test_list_brands_limit_bounds_enforced(call_tool):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json=_brands_payload()))

    with pytest.raises(Exception, match="(?i)limit|51|greater"):
        await call_tool("tires_list_brands", {"limit": 51})
    assert route.call_count == 0


@respx.mock
async def test_list_brands_upstream_error_surfaces_as_tool_error(call_tool):
    respx.get(CATALOG).mock(return_value=httpx.Response(400, json={"price_segment": ['"vip" is not a valid choice.']}))

    with pytest.raises(ToolError, match="vip"):
        await call_tool("tires_list_brands", {"price_segments": ["vip"]})


@respx.mock
async def test_tool_error_never_leaks_secret(call_tool):
    respx.get(CATALOG).mock(
        return_value=httpx.Response(
            500, text=f"gateway fail for https://api.tiresvote.test/v2/tires/catalog/?user_key={TEST_SECRET}"
        )
    )

    with pytest.raises(ToolError) as exc:
        await call_tool("tires_list_brands")
    assert TEST_SECRET not in str(exc.value)


async def test_unmocked_request_is_blocked_by_offline_guard(call_tool):
    # No respx route registered: the conftest transport guard must fire instead
    # of any real network access.
    with pytest.raises(Exception) as exc:
        await call_tool("tires_list_brands")
    assert "offline" in str(exc.value) + str(exc.value.__cause__ or "")


@respx.mock
async def test_two_servers_use_their_own_injected_clients():
    other_base = "https://api.other.test"
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": [], "meta": {"count": 0}}))
    other_route = respx.get(f"{other_base}/v2/tires/catalog/").mock(
        return_value=httpx.Response(200, json=_brands_payload())
    )
    server_a = create_server(client=TiresClient(TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET)))
    server_b = create_server(client=TiresClient(TiresSettings(base_url=other_base, api_key="other-key")))

    ra = await server_a.call_tool("tires_list_brands", {})
    rb = await server_b.call_tool("tires_list_brands", {})

    assert ra.structured_content["total"] == 0
    assert rb.structured_content["total"] == 4
    assert other_route.call_count == 1
