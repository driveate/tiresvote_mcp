"""Inventory synchronization — verified through the registered MCP interface.

Reads the live tool list from a real ``create_server()`` instance (names,
descriptions, parameter schemas, annotations) and compares it with
``docs/tools-inventory.md`` and the generated ``docs/tool-schemas.json``
snapshot. No source-code regex/AST matching.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.conftest import BASE_URL, TEST_SECRET
from tiresvote_mcp.client import TiresClient, TiresSettings
from tiresvote_mcp.server import create_server

DOCS = Path(__file__).parent.parent / "docs"
INVENTORY = DOCS / "tools-inventory.md"
SCHEMA_SNAPSHOT = DOCS / "tool-schemas.json"

EXPECTED_TOOLS = {
    "tires_list_brands",
    "tires_list_brand_tires",
    "tires_get_tire",
    "tires_list_sizes",
    "tires_list_regions",
    "tires_list_performance_categories",
    "tires_search",
    "tires_search_advanced",
    "tires_get_pros_cons",
    "tires_list_materials",
    "tires_list_tests",
    "tires_get_test",
}

_CYRILLIC = re.compile(r"[\u0400-\u04ff]")


def _is_english(text: str) -> bool:
    return bool(text and text.strip()) and not _CYRILLIC.search(text)


async def _registered() -> dict[str, dict]:
    """The exact tool surface a client sees — names, docs, schemas."""
    server = create_server(
        client=TiresClient(
            TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET)
        )
    )
    tools = await server.list_tools()
    out: dict[str, dict] = {}
    for t in tools:
        schema = getattr(t, "parameters", None) or getattr(t, "input_schema", None)
        if schema is None:
            schema = t.inputSchema
        out[t.name] = {
            "description": t.description,
            "tags": sorted(t.tags or []),
            "annotations": t.annotations.model_dump(by_alias=True, exclude_none=True)
            if t.annotations
            else None,
            "input_schema": schema,
        }
    return out


async def test_exactly_the_approved_twelve_tools_registered():
    tools = await _registered()
    assert set(tools) == EXPECTED_TOOLS
    assert all(name.startswith("tires_") for name in tools)


async def test_every_tool_and_parameter_has_english_help():
    tools = await _registered()
    for name, tool in tools.items():
        assert _is_english(tool["description"]), f"{name}: missing/foreign docstring"
        props = tool["input_schema"]["properties"]
        for param, spec in props.items():
            assert _is_english(spec.get("description", "")), (
                f"{name}.{param}: missing or non-English Field description"
            )


async def test_all_tools_read_only_and_tagged():
    tools = await _registered()
    for name, tool in tools.items():
        ann = tool["annotations"]
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False, name
        assert ann["idempotentHint"] is True and ann["openWorldHint"] is False, name
        assert tool["tags"] in (["catalog"], ["search"], ["evidence"])


async def test_inventory_lists_every_tool_and_parameter():
    tools = await _registered()
    text = INVENTORY.read_text()
    for name, tool in tools.items():
        assert f"`{name}`" in text, f"{name} missing from inventory"
        for param in tool["input_schema"]["properties"]:
            assert f"`{param}`" in text, f"{name}.{param} undocumented in inventory"


async def test_registered_schema_snapshot_is_in_sync():
    """docs/tool-schemas.json is the registered surface — regenerate, never edit."""
    tools = await _registered()
    snapshot = json.loads(SCHEMA_SNAPSHOT.read_text())
    assert snapshot == tools, (
        "docs/tool-schemas.json is stale; regenerate it from the registered tools."
    )


async def test_key_contract_points_visible_in_schema():
    tools = await _registered()

    adv = tools["tires_search_advanced"]["input_schema"]["properties"]
    assert "runflat_filter" in adv and "include_runflat" not in adv
    assert "neutral" in adv["runflat_filter"]["description"].lower()
    assert "RunFlat-ONLY" in adv["runflat_filter"]["description"] or "not an exclusion" \
        in adv["runflat_filter"]["description"]

    bnb = tools["tires_get_pros_cons"]["input_schema"]["properties"]
    assert {"buy_offset", "not_buy_offset"} <= set(bnb)
    assert "independent" in bnb["buy_offset"]["description"].lower()

    sizes_spec = tools["tires_get_test"]["input_schema"]["properties"]["sizes"]
    sizes_array = next(b for b in sizes_spec["anyOf"] if b.get("type") == "array")
    assert sizes_array["items"]["maxLength"] == 30  # upstream bound
    assert sizes_array["maxItems"] == 20  # MCP-side bound

    query = tools["tires_search"]["input_schema"]["properties"]["query"]
    assert query["maxLength"] == 100

    detail = tools["tires_get_tire"]["input_schema"]["properties"]["detail"]
    assert set(detail["enum"]) == {"concise", "full"}
    assert detail["default"] == "concise"


async def test_array_parameters_carry_the_documented_mcp_bound():
    tools = await _registered()
    for name, tool in tools.items():
        props = tool["input_schema"]["properties"]
        for param, spec in props.items():
            spec_str = json.dumps(spec)
            if '"array"' in spec_str or spec.get("type") == "array":
                # resolve through anyOf wrappers
                assert '"maxItems": 20' in spec_str, f"{name}.{param} lacks maxItems 20"
