"""Opt-in live smoke suite against the production Tires API.

These tests are marked ``integration`` and run ONLY when BOTH conditions hold:

- pytest is invoked with ``--run-live``, and
- ``WHEELSIZE_API_KEY`` is set in the environment (read from env only — no
  credential is stored, probed or printed by this suite).

Ordinary pytest runs skip this module entirely and keep the offline network
guard engaged. When enabled, the conftest guard disengages for these tests
alone; every tool call below is a real HTTPS GET to the production API through
a registered FastMCP ``Client``.

Scope is deliberately bounded: one call per tool (12 GETs total at
``max_retries=0``), tiny limits, and shape/provenance assertions only — no
volatile counts, scores or availability claims, and no physical-fitment
judgements. Legitimately empty evidence (e.g. ``data: null`` pros/cons) is
accepted, not treated as failure.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastmcp import Client

from tiresvote_mcp.client import TiresClient, TiresSettings
from tiresvote_mcp.server import create_server

pytestmark = pytest.mark.integration

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
EXPECTED_PROMPTS = {
    "tire_selection_by_size",
    "tire_comparison",
    "tire_test_explainer",
    "tire_model_brief",
}

# Control identities verified live by the live investigator (docs/live-validation.md).
BRAND = "michelin"
PRODUCT = "pilot-sport-4"

_SITE_HOSTS = {"tiresvote.com", "www.tiresvote.com"}


@pytest.fixture
def live_key() -> str:
    key = os.environ.get("WHEELSIZE_API_KEY", "")
    if not key:
        pytest.skip("WHEELSIZE_API_KEY is not set")
    return key


@pytest.fixture
async def live_mcp(live_key):
    """FastMCP Client over the real server stack; deterministic budget.

    ``max_retries=0`` pins each tool call to exactly one upstream GET, so the
    scenario below is 12 HTTP requests at most.
    """
    settings = TiresSettings(api_key=live_key, max_retries=0, timeout=30.0)
    client = TiresClient(settings)
    server = create_server(client=client)
    async with Client(server) as mcp:
        yield mcp
    await client.aclose()


def _payload(result) -> dict[str, Any]:
    """Return a tool result's structured payload; must be a JSON object."""
    assert result.structured_content is not None, "tool returned no structured content"
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def _assert_clean(data: Any, key: str) -> None:
    """No surfaced payload may contain the key or key-bearing URLs."""
    rendered = json.dumps(data)
    # Avoid pytest assertion rewriting echoing the credential on failure.
    if key in rendered:
        raise AssertionError("A credential appeared in the tool response")
    assert "user_key" not in rendered


def _assert_envelope(data: dict[str, Any]) -> list:
    """Common list envelope contract shared by all list-style tools."""
    results = data.get("results")
    assert isinstance(results, list)
    assert isinstance(data.get("has_more"), bool)
    assert "page" in data or "offset" in data  # one of the two policies
    return results


def _assert_citation(url: Any) -> None:
    """A surfaced link is a citation: absent-or-tiresvote.com http(s)."""
    if url is None:
        return
    assert isinstance(url, str)
    parts = urlsplit(url)
    assert parts.scheme in ("http", "https")
    assert parts.hostname in _SITE_HOSTS


async def test_registered_surface_and_status(live_mcp, live_key):
    """tools/list, prompts/list and config://status — no upstream GET needed."""
    tools = await live_mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert len(tools) == 12
    for tool in tools:
        assert tool.annotations and tool.annotations.read_only_hint is True
        assert tool.description

    prompts = await live_mcp.list_prompts()
    assert {p.name for p in prompts} == EXPECTED_PROMPTS

    contents = await live_mcp.read_resource("config://status")
    payload = json.loads(contents[0].text)
    assert payload["server"] == "tiresvote"
    assert payload["api_key_configured"] is True
    assert payload["api_base_url"] == "https://api.wheel-size.com"
    _assert_clean(payload, live_key)


async def test_all_twelve_tools_one_call_each(live_mcp, live_key):
    """One GET per tool — 12 upstream requests total — via registered calls."""
    seen: dict[str, dict] = {}

    async def call(name: str, args: dict) -> dict:
        data = _payload(await live_mcp.call_tool(name, args))
        _assert_clean(data, live_key)
        seen[name] = data
        return data

    # -- catalog ------------------------------------------------------------
    brands = _assert_envelope(await call("tires_list_brands", {"limit": 5}))
    for row in brands:
        assert isinstance(row["slug"], str) and row["slug"]
        assert isinstance(row["display"], str) and row["display"]
    assert any(row["slug"] == BRAND for row in brands) or len(brands) == 5

    brand_tires = _assert_envelope(
        await call("tires_list_brand_tires", {"brand": BRAND, "limit": 3})
    )
    for row in brand_tires:
        assert isinstance(row.get("slug"), str) and row["slug"]
        _assert_citation(row.get("canonical_link"))
    assert "available_count" in seen["tires_list_brand_tires"]

    tire = await call("tires_get_tire", {"brand": BRAND, "product": PRODUCT})
    assert tire.get("slug") == PRODUCT  # requested identity echoed
    brand_ref = tire.get("brand")
    if isinstance(brand_ref, dict):
        assert brand_ref.get("slug") == BRAND
    _assert_citation(tire.get("canonical_link"))  # citation field, not "link"

    sizes = _assert_envelope(
        await call("tires_list_sizes", {"brand": BRAND, "product": PRODUCT, "limit": 3})
    )
    for row in sizes:
        # Designation text plus geometry — values may be null per sizing_system.
        assert isinstance(row.get("text"), str) and row["text"]
        assert "sizing_system" in row
        for dim in ("tire_width", "rim_diameter"):
            assert row.get(dim) is None or isinstance(row[dim], (int, float))

    # -- search ---------------------------------------------------------------
    search = _assert_envelope(
        await call("tires_search", {"query": "pilot sport", "per_page": 2})
    )
    for row in search:
        assert isinstance(row.get("slug"), str) and row["slug"]
        _assert_citation(row.get("canonical_link"))

    advanced = _assert_envelope(
        await call(
            "tires_search_advanced",
            {"brands": [BRAND], "runflat_filter": True, "per_page": 2},
        )
    )
    for row in advanced:
        assert isinstance(row.get("slug"), str) and row["slug"]

    # -- evidence -------------------------------------------------------------
    bnb = await call(
        "tires_get_pros_cons", {"brand": BRAND, "product": PRODUCT, "limit": 2}
    )
    # Two distinct evidence states: ``data: null`` surfaces as BOTH sides null
    # (absent evidence — no approved reasons), otherwise each side is its own
    # envelope that may legitimately hold zero reasons (an empty collection).
    assert "buy" in bnb and "not_buy" in bnb
    buy, not_buy = bnb["buy"], bnb["not_buy"]
    if buy is None and not_buy is None:
        assert "note" in bnb  # absent-evidence marker
    else:
        assert isinstance(buy, dict) and isinstance(not_buy, dict)
        for side in (buy, not_buy):
            assert isinstance(side.get("reasons"), list)
            assert isinstance(side.get("total"), int)
            assert isinstance(side.get("has_more"), bool)
            for reason in side["reasons"]:
                assert isinstance(reason.get("text"), str) and reason["text"]
                assert "prooflink" in reason and "upvotes" in reason
                if reason["prooflink"]:
                    assert isinstance(reason["prooflink"], str)

    materials = _assert_envelope(
        await call(
            "tires_list_materials", {"brand": BRAND, "product": PRODUCT, "limit": 2}
        )
    )
    for row in materials:
        assert isinstance(row, dict) and isinstance(row.get("type"), str)

    tests = _assert_envelope(
        await call("tires_list_tests", {"per_page": 1})
    )
    # The professional-tests catalog is never legitimately empty upstream.
    assert tests, "tires_list_tests returned no rows — unexpected for production"
    slug = tests[0]["slug"]
    assert isinstance(slug, str) and slug
    _assert_citation(tests[0].get("canonical_link"))

    detail = await call("tires_get_test", {"slug": slug, "limit": 2})
    # Header identity survives independent of participant slicing.
    header = detail.get("test")
    assert isinstance(header, dict)
    assert header.get("slug") == slug
    assert isinstance(header.get("title"), str) and header["title"]
    _assert_citation(header.get("canonical_link"))
    assert "sizes_checked" in detail
    participants = detail.get("participants")
    assert isinstance(participants, dict)
    assert isinstance(participants.get("results"), list)
    for row in participants["results"]:
        product = row.get("product")
        assert isinstance(product, dict) and isinstance(product.get("slug"), str)
        _assert_citation(product.get("canonical_link"))
        assert "test_score" in row and "recommend" in row and "place" in row

    # -- references -----------------------------------------------------------
    regions = _assert_envelope(await call("tires_list_regions", {"limit": 3}))
    for row in regions:
        assert isinstance(row.get("slug"), str) and row["slug"]

    categories = _assert_envelope(
        await call("tires_list_performance_categories", {"limit": 3})
    )
    for row in categories:
        assert isinstance(row.get("slug"), str) and row["slug"]

    # The scenario made exactly one call per tool — never more than the budget.
    assert set(seen) == EXPECTED_TOOLS
    assert len(seen) == 12
