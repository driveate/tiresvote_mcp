"""Offline tests for response unwrapping, projections and pagination policies."""

import pytest
from fastmcp.exceptions import ToolError

from tests.conftest import BASE_URL, TEST_SECRET
from tiresvote_mcp.response import (
    api_page_envelope,
    map_brand,
    slice_envelope,
    unwrap_report,
)

SEARCH = "/v2/tires/search/"


def _api_env(payload, page=1, per_page=10, map_item=lambda x: x, path=SEARCH):
    return api_page_envelope(
        payload,
        page=page,
        per_page=per_page,
        map_item=map_item,
        api_base_url=BASE_URL,
        expected_path=path,
        secrets=[TEST_SECRET],
    )


# ---------------------------------------------------------------------------
# Envelope unwrapping
# ---------------------------------------------------------------------------


def test_unwrap_report_returns_data_and_meta():
    data, meta = unwrap_report({"data": [1], "meta": {"count": 1}})
    assert data == [1] and meta == {"count": 1}


def test_unwrap_report_missing_meta_is_tolerated():
    assert unwrap_report({"data": []}) == ([], {})


@pytest.mark.parametrize("bad", [[1, 2], "x", 5, None, {"items": []}])
def test_unwrap_report_rejects_wrong_shapes(bad):
    with pytest.raises(ToolError):
        unwrap_report(bad)


@pytest.mark.parametrize("bad_meta", [["x"], "meta", 5, True])
def test_unwrap_report_wrong_type_meta_is_contract_error(bad_meta):
    with pytest.raises(ToolError, match="meta"):
        unwrap_report({"data": [], "meta": bad_meta})


# ---------------------------------------------------------------------------
# API-pages policy: consecutive same-origin same-endpoint next only
# ---------------------------------------------------------------------------


def _payload(next_link=None, items=None):
    return {
        "data": items if items is not None else [{"slug": "a"}],
        "meta": {
            "pagination": {
                "current_page_count": 1,
                "total_items": 30,
                "total_pages": 3,
                "links": {"first": f"{BASE_URL}{SEARCH}?page=1"},
                "next": next_link,
            }
        },
    }


def test_api_page_envelope_clean_next_authorizes_numeric_page():
    next_link = f"{BASE_URL}{SEARCH}?q=x&page=2&per_page=10"
    env = _api_env(_payload(next_link))

    assert env["has_more"] is True
    assert env["next_page"] == 2
    assert env["total_items"] == 30
    # The URL itself is never surfaced — only the numeric argument.
    assert next_link not in str(env)


def test_api_page_envelope_relative_next_same_path_authorized():
    env = _api_env(_payload("/v2/tires/search/?page=2"))
    assert env["has_more"] is True and env["next_page"] == 2


@pytest.mark.parametrize(
    "link",
    [
        f"{BASE_URL}{SEARCH}?page=99",  # skipped page
        f"{BASE_URL}{SEARCH}?page=1",  # loops back to same page
        f"{BASE_URL}{SEARCH}?page=-2",  # negative page
        f"{BASE_URL}{SEARCH}?page=abc",  # non-numeric
        f"{BASE_URL}{SEARCH}",  # no page param
        f"{BASE_URL}/v2/tires/tests/?page=2",  # different endpoint
        f"{BASE_URL}{SEARCH}../catalog/?page=2",  # traversal-ish path
        "https://evil.test/v2/tires/search/?page=2",  # other host
        "http://api.tiresvote.test/v2/tires/search/?page=2",  # scheme downgrade
        "https://user:pw@api.tiresvote.test/v2/tires/search/?page=2",  # creds
        f"{BASE_URL}:9999/v2/tires/search/?page=2",  # other port
        "not a url",
        12345,
        "",
    ],
)
def test_api_page_envelope_suspicious_next_not_followed(link):
    env = _api_env(_payload(link))
    assert env["has_more"] is False
    assert env["pagination_limited"] is True
    assert "site_url" not in env or env["site_url"] == ""
    # The link is never surfaced or fetchable.
    if isinstance(link, str) and link:
        assert link not in str(env)


def test_api_page_envelope_no_next_means_last_page():
    env = _api_env(_payload(None))
    assert env["has_more"] is False and env["pagination_limited"] is False
    assert "next_page" not in env


def test_api_page_envelope_html_site_handoff_is_limited_navigation():
    site = "https://tiresvote.com/tires/?q=x&page=5"
    env = _api_env(_payload(site))

    assert env["has_more"] is False
    assert env["pagination_limited"] is True
    assert env["site_url"] == site
    assert "next_page" not in env


def test_api_page_envelope_site_url_is_scrubbed():
    site = f"https://tiresvote.com/tires/?user_key={TEST_SECRET}&page=5"
    env = _api_env(_payload(site))
    assert TEST_SECRET not in str(env)
    assert "user_key" not in env["site_url"]  # credential params dropped
    assert "page=5" in env["site_url"]  # navigation params preserved


def test_api_page_envelope_missing_pagination_is_tolerated():
    env = _api_env({"data": [{"slug": "a"}], "meta": {}})
    assert env["has_more"] is False


@pytest.mark.parametrize("bad", [["x"], "pag", 5, True])
def test_api_page_envelope_wrong_type_pagination_is_contract_error(bad):
    with pytest.raises(ToolError, match="pagination"):
        _api_env({"data": [], "meta": {"pagination": bad}})


def test_api_page_envelope_non_list_data_is_contract_error():
    with pytest.raises(ToolError, match="list"):
        _api_env({"data": {"slug": "a"}, "meta": {}})


# ---------------------------------------------------------------------------
# MCP-slice policy: local limit/offset over a complete upstream list
# ---------------------------------------------------------------------------


def test_slice_envelope_mid_list_reports_next_offset():
    payload = {"data": [{"slug": f"b{i}"} for i in range(6)], "meta": {}}
    env = slice_envelope(payload, limit=3, offset=2, map_item=lambda x: x)

    assert [r["slug"] for r in env["results"]] == ["b2", "b3", "b4"]
    assert env["available_count"] == 6 and env["total"] == 6
    assert env["has_more"] is True and env["next_offset"] == 5
    assert env["truncated"] is False


def test_slice_envelope_last_slice_reports_no_more():
    payload = {"data": [1, 2, 3, 4, 5], "meta": {}}
    env = slice_envelope(payload, limit=50, offset=4, map_item=lambda x: x)
    assert env["results"] == [5] and env["has_more"] is False and "next_offset" not in env


def test_slice_envelope_beyond_end_is_empty_but_not_more():
    env = slice_envelope({"data": [1], "meta": {}}, limit=10, offset=9, map_item=lambda x: x)
    assert env["results"] == [] and env["has_more"] is False


def test_slice_envelope_truncated_when_count_exceeds_available():
    payload = {"data": [{"slug": f"b{i}"} for i in range(200)], "meta": {"count": 400}}
    env = slice_envelope(payload, limit=50, offset=0, map_item=lambda x: x)

    assert env["truncated"] is True
    assert env["total"] == 400 and env["available_count"] == 200
    assert env["has_more"] is True and env["next_offset"] == 50
    assert "hint" in env


def test_slice_envelope_more_than_200_brand_rows_still_navigable():
    payload = {"data": [{"slug": f"b{i:03d}", "display": str(i)} for i in range(250)], "meta": {"count": 400}}
    env = slice_envelope(payload, limit=50, offset=100, map_item=lambda x: x)
    assert env["has_more"] is True and env["next_offset"] == 150
    env2 = slice_envelope(payload, limit=50, offset=200, map_item=lambda x: x)
    assert env2["has_more"] is False
    assert env2["truncated"] is True  # count=400 but only 250 rows arrived


def test_slice_envelope_non_list_data_is_contract_error():
    with pytest.raises(ToolError, match="list"):
        slice_envelope({"data": {"a": 1}, "meta": {}}, limit=5, offset=0, map_item=lambda x: x)


def test_slice_envelope_non_numeric_count_falls_back_to_available():
    env = slice_envelope(
        {"data": [1, 2], "meta": {"count": "many"}}, limit=5, offset=0, map_item=lambda x: x
    )
    assert env["total"] == 2 and env["truncated"] is False


# ---------------------------------------------------------------------------
# map_brand projection: required identity, preserved nullability
# ---------------------------------------------------------------------------


def test_map_brand_projects_fields():
    assert map_brand(
        {"slug": "michelin", "display": "Michelin", "price_segment": "premium", "products_count": 144}
    ) == {
        "slug": "michelin",
        "display": "Michelin",
        "price_segment": "premium",
        "products_count": 144,
    }


def test_map_brand_nullable_price_segment_preserved():
    row = map_brand({"slug": "x", "display": "X", "price_segment": None, "products_count": 0})
    assert row["price_segment"] is None


@pytest.mark.parametrize(
    "row",
    [
        {},  # no identity at all — must not become an all-null brand
        {"slug": "x"},  # missing display
        {"display": "X"},  # missing slug
        {"slug": "", "display": "X"},
        {"slug": "x", "display": ""},
        "michelin",
        None,
    ],
)
def test_map_brand_requires_identity(row):
    with pytest.raises(ToolError):
        map_brand(row)
