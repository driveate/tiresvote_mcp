"""Offline tests for TiresClient: request shaping, retries, errors, redaction.

HTTP is mocked with respx (or an injected MockTransport) — no network needed.
The suite-level guard in conftest.py fails any request that would escape mocks
at the socket boundary.
"""

import asyncio
import logging
import traceback

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

import tiresvote_mcp.client as client_module
from tests.conftest import BASE_URL, TEST_SECRET
from tiresvote_mcp.client import TiresSettings

CATALOG = f"{BASE_URL}/v2/tires/catalog/"


def _params(request: httpx.Request) -> httpx.QueryParams:
    return httpx.QueryParams(request.url.query)


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------


@respx.mock
async def test_user_key_added_and_none_params_stripped(client):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))

    await client.get("/v2/tires/catalog/", {"price_segment": None, "ordering": "slug"})

    params = _params(route.calls.last.request)
    assert params.get("user_key") == TEST_SECRET
    assert params.get("ordering") == "slug"
    assert "price_segment" not in params


@respx.mock
async def test_no_user_key_when_key_missing(make_client):
    client = make_client(TiresSettings(base_url=BASE_URL, api_key=""))
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))

    await client.get("/v2/tires/catalog/")

    assert "user_key" not in _params(route.calls.last.request)


@respx.mock
async def test_list_params_become_repeated_query_keys(client):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))

    await client.get("/v2/tires/catalog/", {"price_segment": ["premium", "economy"]})

    assert _params(route.calls.last.request).get_list("price_segment") == ["premium", "economy"]


@respx.mock
async def test_bool_false_is_sent_not_omitted(client):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))

    await client.get("/v2/tires/catalog/", {"runflat": False, "show_oe": True})

    params = _params(route.calls.last.request)
    assert params.get("runflat") == "false"
    assert params.get("show_oe") == "true"


@respx.mock
async def test_host_header_override_sent(make_client):
    settings = TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET, host_header="api.internal.test")
    client = make_client(settings)
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))

    await client.get("/v2/tires/catalog/")

    assert route.calls.last.request.headers["host"] == "api.internal.test"


@respx.mock
async def test_no_host_header_by_default(client):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))

    await client.get("/v2/tires/catalog/")

    assert route.calls.last.request.headers.get("host") in (None, "api.tiresvote.test")


async def test_injected_mock_transport_is_used(make_client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": ["ok"], "meta": {}})

    client = make_client(transport=httpx.MockTransport(handler))

    assert await client.get("/v2/tires/catalog/") == {"data": ["ok"], "meta": {}}
    assert seen["path"] == "/v2/tires/catalog/"


# ---------------------------------------------------------------------------
# Settings validation and safe representation
# ---------------------------------------------------------------------------


def test_base_url_accepts_plain_origin():
    assert TiresSettings(base_url="https://api.test/").base_url == "https://api.test"
    assert TiresSettings(base_url="http://127.0.0.1:8080").base_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    "bad",
    [
        "https://username:password@example.test",  # credentials
        "https://user@example.test",
        "ftp://x.test",  # scheme
        "https://x.test/some/path",  # path
        "https://x.test/?user_key=abc",  # query
        "https://x.test#frag",
        "https://x.test:99999",  # invalid port
        "not a url",
        "",
        "https://",
    ],
)
def test_base_url_rejects_non_origin(bad):
    with pytest.raises(ValueError) as exc:
        TiresSettings(base_url=bad)
    # The unsafe input is never echoed back.
    if bad:
        assert bad not in str(exc.value)


def test_base_url_error_never_leaks_embedded_secret():
    poisoned = f"https://example.test/?user_key={TEST_SECRET}"
    with pytest.raises(ValueError) as exc:
        TiresSettings(base_url=poisoned, api_key=TEST_SECRET)
    assert TEST_SECRET not in str(exc.value)


def test_settings_repr_redacts_api_key():
    settings = TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET)
    assert TEST_SECRET not in repr(settings)
    assert TEST_SECRET not in str(settings)


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), -0.5])
def test_timeout_must_be_finite_positive(bad):
    with pytest.raises(ValueError):
        TiresSettings(timeout=bad)


@pytest.mark.parametrize("bad", [-1, 3, 100, 1.5, "2", True])
def test_max_retries_bounded(bad):
    with pytest.raises(ValueError):
        TiresSettings(max_retries=bad)


@pytest.mark.parametrize("bad", ["api\n.test", "api host.test", "a;b", "x:80/x"])
def test_host_header_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        TiresSettings(host_header=bad)


# ---------------------------------------------------------------------------
# Path allowlist and slug parameters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v2/tires/articles/",  # excluded upstream route
        "/v2/tires/top-charts/",  # excluded upstream route
        "/v2/tires/unknown/",  # not in the inventory at all
        "/v2/tires/catalogx/",  # prefix look-alike
        "/v2/tires/",  # bare prefix
        "/v2/other/catalog/",
        "https://evil.test/v2/tires/catalog/",  # absolute URL
        "//evil.test/v2/tires/",
        "/v2/tires/catalog",  # missing trailing slash
    ],
)
async def test_non_allowlisted_paths_rejected(client, path):
    with pytest.raises(ToolError, match="approved"):
        await client.get(path)


async def test_excluded_route_with_slug_rejected(client):
    with pytest.raises(ToolError, match="approved"):
        await client.get("/v2/tires/top-charts/{slug}/", slug="x")


@pytest.mark.parametrize("bad", ["a/b", "..", "../x", "a b", "a?b", "a%2e", "", "bǟd", "x.y"])
async def test_slug_path_params_rejected(client, bad):
    with pytest.raises(ToolError, match="brand"):
        await client.get("/v2/tires/catalog/{brand}/", brand=bad)


async def test_slug_error_never_contains_secret(client):
    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/{brand}/", brand=f"{TEST_SECRET}/../x")
    assert TEST_SECRET not in str(exc.value)


@respx.mock
async def test_slug_path_params_substituted(client):
    route = respx.get(f"{BASE_URL}/v2/tires/catalog/michelin/").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {}})
    )

    await client.get("/v2/tires/catalog/{brand}/", brand="michelin")

    assert route.calls.last.request.url.path == "/v2/tires/catalog/michelin/"


async def test_unfilled_placeholder_rejected(client):
    with pytest.raises(ToolError):
        await client.get("/v2/tires/catalog/{brand}/")


async def test_unknown_path_param_rejected(client):
    # Parameters that are not part of the template are a caller bug — reject
    # instead of silently ignoring them.
    with pytest.raises(ToolError, match="no parameters"):
        await client.get("/v2/tires/catalog/", bogus="x")


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@respx.mock
async def test_retryable_status_then_success(client, status):
    route = respx.get(CATALOG).mock(
        side_effect=[
            httpx.Response(status, text="busy"),
            httpx.Response(200, json={"data": []}),
        ]
    )

    assert await client.get("/v2/tires/catalog/") == {"data": []}
    assert route.call_count == 2


@respx.mock
async def test_retries_exhausted_reports_last_status(client):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(503, text="down"))

    with pytest.raises(ToolError, match="503"):
        await client.get("/v2/tires/catalog/")
    assert route.call_count == 3  # first attempt + 2 retries


@respx.mock
async def test_429_exhausted_reports_rate_limit_and_attempts(client):
    respx.get(CATALOG).mock(return_value=httpx.Response(429, json={"detail": "throttled"}))

    with pytest.raises(ToolError, match=r"Rate limited .* after 3 attempts"):
        await client.get("/v2/tires/catalog/")


@respx.mock
async def test_zero_retries_means_single_attempt(make_client):
    client = make_client(
        TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET, max_retries=0)
    )
    route = respx.get(CATALOG).mock(return_value=httpx.Response(503, text="down"))

    with pytest.raises(ToolError, match="503"):
        await client.get("/v2/tires/catalog/")
    assert route.call_count == 1


@respx.mock
async def test_one_retry_means_two_attempts(make_client):
    client = make_client(
        TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET, max_retries=1)
    )
    route = respx.get(CATALOG).mock(return_value=httpx.Response(503, text="down"))

    with pytest.raises(ToolError, match="503"):
        await client.get("/v2/tires/catalog/")
    assert route.call_count == 2


@respx.mock
async def test_transport_error_retried_then_reported(client):
    route = respx.get(CATALOG).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ToolError, match="Network error"):
        await client.get("/v2/tires/catalog/")
    assert route.call_count == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
async def test_client_errors_not_retried(client, status):
    route = respx.get(CATALOG).mock(return_value=httpx.Response(status, json={"detail": "x"}))

    with pytest.raises(ToolError):
        await client.get("/v2/tires/catalog/")
    assert route.call_count == 1


@pytest.mark.parametrize("value", ["120", "0.25"])
def test_retry_delay_honors_and_caps_retry_after(value):
    delay = client_module._retry_delay(httpx.Response(429, headers={"Retry-After": value}), 0)
    assert delay == min(float(value), 5.0)


@pytest.mark.parametrize("value", ["abc", "-5", "nan", "inf", "-inf", ""])
def test_retry_delay_invalid_values_fall_back(value):
    # BACKOFF_BASE is zeroed by conftest, so any fallback is 0.0 — the point is
    # that invalid Retry-After never produces NaN/negative/unbounded sleeps.
    delay = client_module._retry_delay(httpx.Response(429, headers={"Retry-After": value}), 0)
    assert delay == 0.0


def test_retry_delay_backoff_grows_without_header():
    # BACKOFF_BASE is zeroed by conftest; check the formula against a real base.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(client_module, "BACKOFF_BASE", 0.5)
        assert client_module._retry_delay(httpx.Response(200), 0) == 0.5
        assert client_module._retry_delay(httpx.Response(200), 1) == 1.0
        assert client_module._retry_delay(httpx.Response(200), 2) == 2.0


# ---------------------------------------------------------------------------
# Error bodies (DRF and gateway)
# ---------------------------------------------------------------------------


@respx.mock
async def test_400_field_errors_mapped_to_mcp_names(client):
    respx.get(CATALOG).mock(
        return_value=httpx.Response(400, json={"reg": ['"xx" is not a valid choice.']})
    )

    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/", {"reg": ["xx"]})
    msg = str(exc.value)
    assert "regions" in msg and "reg" in msg
    assert "tires_list_regions" in msg


@respx.mock
async def test_400_rf_maps_to_runflat_filter(client):
    respx.get(CATALOG).mock(return_value=httpx.Response(400, json={"rf": ["invalid value"]}))

    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/")
    assert "runflat_filter" in str(exc.value)


@respx.mock
async def test_400_detail_and_non_field_errors(client):
    respx.get(CATALOG).mock(
        side_effect=[
            httpx.Response(400, json={"non_field_errors": ["filters conflict"]}),
            httpx.Response(400, json={"detail": "bad combination"}),
        ]
    )

    with pytest.raises(ToolError, match="filters conflict"):
        await client.get("/v2/tires/catalog/")
    with pytest.raises(ToolError, match="bad combination"):
        await client.get("/v2/tires/catalog/")


@respx.mock
async def test_400_list_body(client):
    respx.get(CATALOG).mock(return_value=httpx.Response(400, json=["first", "second"]))

    with pytest.raises(ToolError, match="first"):
        await client.get("/v2/tires/catalog/")


@respx.mock
async def test_400_field_with_scalar_value_tolerated(client):
    respx.get(CATALOG).mock(return_value=httpx.Response(400, json={"brand": "required"}))

    with pytest.raises(ToolError, match="brand"):
        await client.get("/v2/tires/catalog/")


@respx.mock
async def test_404_reports_not_found_with_hint(client):
    respx.get(CATALOG).mock(return_value=httpx.Response(404, json={"detail": "Not found."}))

    with pytest.raises(ToolError, match="Not found"):
        await client.get("/v2/tires/catalog/")


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_plain_text_auth_errors_mention_env_var(client, status):
    respx.get(CATALOG).mock(
        return_value=httpx.Response(status, text="Authentication parameters missing")
    )

    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/")
    assert "WHEELSIZE_API_KEY" in str(exc.value)


@respx.mock
async def test_non_json_error_body_is_truncated_and_scrubbed(client):
    body = f"<html>fail url=https://x/?user_key={TEST_SECRET}&p=1</html>" + "x" * 500
    respx.get(CATALOG).mock(return_value=httpx.Response(500, text=body))

    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/")
    msg = str(exc.value)
    assert "500" in msg and len(msg) < 500
    assert TEST_SECRET not in msg and f"user_key={TEST_SECRET}" not in msg


# ---------------------------------------------------------------------------
# Malformed success responses
# ---------------------------------------------------------------------------


@respx.mock
async def test_200_with_non_json_body(client):
    respx.get(CATALOG).mock(return_value=httpx.Response(200, text="<html>oops</html>"))

    with pytest.raises(ToolError, match="not valid JSON"):
        await client.get("/v2/tires/catalog/")


@respx.mock
async def test_200_non_json_snippet_is_scrubbed_before_truncation(client):
    # Secret sits past the truncation boundary; scrubbing must run first so no
    # partial key can leak into the error snippet.
    body = "x" * 200 + f"?user_key={TEST_SECRET}" + "y" * 200
    respx.get(CATALOG).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/")
    assert TEST_SECRET not in str(exc.value)


@respx.mock
async def test_200_json_is_scrubbed_before_return(client):
    leaky = {
        "data": [{"link": f"https://api.tiresvote.test/v2/tires/x/?user_key={TEST_SECRET}&p=2"}],
        "meta": {"note": f"see user_key={TEST_SECRET}"},
    }
    respx.get(CATALOG).mock(return_value=httpx.Response(200, json=leaky))

    result = await client.get("/v2/tires/catalog/")

    assert TEST_SECRET not in str(result)
    assert "user_key" not in result["data"][0]["link"]
    assert "user_key=***" in result["meta"]["note"]


@respx.mock
async def test_transport_error_message_and_traceback_never_contain_secret(client):
    respx.get(CATALOG).mock(side_effect=httpx.ConnectError(f"boom {TEST_SECRET}"))

    with pytest.raises(ToolError) as exc:
        await client.get("/v2/tires/catalog/")

    rendered = "".join(traceback.format_exception(exc.value))
    assert TEST_SECRET not in str(exc.value)
    assert TEST_SECRET not in rendered  # exception chains stay clean
    assert exc.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# Logging and lifecycle
# ---------------------------------------------------------------------------


async def test_httpx_request_logs_never_contain_secret(make_client, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = make_client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO, logger="httpx"):
        await client.get("/v2/tires/catalog/", {"q": "icecontact"})

    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP Request" in rendered  # httpx did log the request line
    assert TEST_SECRET not in rendered
    assert "user_key" in rendered  # logged URL is masked, not dropped silently


async def test_httpx_error_level_logs_never_contain_secret(make_client, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {TEST_SECRET}", request=request)

    client = make_client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="httpx"):
        with pytest.raises(ToolError):
            await client.get("/v2/tires/catalog/")

    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_SECRET not in rendered


async def test_aclose_releases_pooled_client(make_client):
    client = make_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
    )
    await client.get("/v2/tires/catalog/")
    pooled = client._client
    assert pooled is not None and not pooled.is_closed

    await client.aclose()
    assert pooled.is_closed

    await client.aclose()  # idempotent


async def test_stale_client_closed_or_detached_on_loop_change(client):
    # A client pooled on a different (now closed) loop must not leak or break.
    # The other loop runs in a thread so this test's loop stays running.
    import threading

    errors = []

    def warm_on_other_loop():
        try:
            with respx.mock:
                respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))
                asyncio.run(client.get("/v2/tires/catalog/"))
        except Exception as e:  # pragma: no cover - asserted below
            errors.append(e)

    thread = threading.Thread(target=warm_on_other_loop)
    thread.start()
    thread.join()
    assert not errors

    first = client._client
    assert first is not None  # pooled client bound to the now-closed loop

    with respx.mock:
        respx.get(CATALOG).mock(return_value=httpx.Response(200, json={"data": []}))
        await client.get("/v2/tires/catalog/")

    # The swap bound a fresh AsyncClient to this test's loop.
    assert client._client is not first
    assert client._loop is asyncio.get_running_loop()
    await first.aclose()  # clean up the detached client (still closable here)
