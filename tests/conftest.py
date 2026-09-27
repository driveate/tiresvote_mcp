"""Shared fixtures: offline network blocking, client/server builders, call_tool.

The offline suite must never touch the network. The guard lives at the socket
boundary — ``socket.connect``/``connect_ex``/``create_connection`` and the
resolver functions — so it cannot interfere with respx's httpx-level patching
and also catches accidental urllib/raw-socket use. AF_UNIX (event-loop pipes)
and loopback destinations stay reachable; respx mocks and injected
MockTransports never hit the socket layer anyway.

Live integration tests (``@pytest.mark.integration``) are the only exception:
they run solely when BOTH ``--run-live`` is passed AND ``WHEELSIZE_API_KEY`` is
set in the environment. Ordinary pytest runs — even with the key present —
skip them and keep the network guard fully engaged.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

import pytest

from tiresvote_mcp.client import TiresClient, TiresSettings
from tiresvote_mcp.server import create_server

BASE_URL = "https://api.tiresvote.test"
# Synthetic key used to prove redaction — never a real credential.
TEST_SECRET = "tvs-test-secret-9f2c7a1d"

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex
_real_getnameinfo = socket.getnameinfo

_LOCAL_NAMES = {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}


def _is_local(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    return host in _LOCAL_NAMES


def _offline_error(where: str, dest: Any) -> AssertionError:
    return AssertionError(f"offline tests must not use the network ({where}: {dest!r})")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked 'integration' against the live Tires API "
        "(requires WHEELSIZE_API_KEY in the environment)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration tests unless BOTH --run-live and WHEELSIZE_API_KEY apply."""
    if config.getoption("--run-live") and os.environ.get("WHEELSIZE_API_KEY"):
        return
    reason = (
        "live integration tests require --run-live"
        if not config.getoption("--run-live")
        else "--run-live given but WHEELSIZE_API_KEY is not set in the environment"
    )
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch, request):
    """Block AF_INET/AF_INET6 egress at the socket/DNS boundary for all tests.

    The guard disengages ONLY for a test marked 'integration' while the suite
    runs with --run-live — every other test keeps hard egress blocking.
    """
    is_live = (
        request.node.get_closest_marker("integration") is not None
        and request.config.getoption("--run-live")
    )
    if is_live:
        # Still zero the retry seam: deterministic, and retries are disabled in
        # the integration settings anyway (max_retries=0).
        monkeypatch.setattr("tiresvote_mcp.client.BACKOFF_BASE", 0.0)
        return

    def _guarded_connect(sock: socket.socket, address: Any):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) else address
            if not _is_local(host):
                raise _offline_error("connect", address)
        return _real_connect(sock, address)

    def _guarded_connect_ex(sock: socket.socket, address: Any):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) else address
            if not _is_local(host):
                raise _offline_error("connect_ex", address)
        return _real_connect_ex(sock, address)

    def _guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local(host):
            raise _offline_error("create_connection", address)
        return _real_create_connection(address, *args, **kwargs)

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise _offline_error("getaddrinfo", host)
        return _real_getaddrinfo(host, *args, **kwargs)

    def _guarded_gethostbyname(host):
        if not _is_local(host):
            raise _offline_error("gethostbyname", host)
        return _real_gethostbyname(host)

    def _guarded_gethostbyname_ex(host):
        if not _is_local(host):
            raise _offline_error("gethostbyname_ex", host)
        return _real_gethostbyname_ex(host)

    def _guarded_getnameinfo(sockaddr, flags):
        host = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
        if not _is_local(host):
            raise _offline_error("getnameinfo", sockaddr)
        return _real_getnameinfo(sockaddr, flags)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", _guarded_gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", _guarded_gethostbyname_ex)
    monkeypatch.setattr(socket, "getnameinfo", _guarded_getnameinfo)

    # Zero the client's retry backoff at the client's own seam — global
    # asyncio.sleep stays untouched.
    monkeypatch.setattr("tiresvote_mcp.client.BACKOFF_BASE", 0.0)


@pytest.fixture
def settings() -> TiresSettings:
    return TiresSettings(base_url=BASE_URL, api_key=TEST_SECRET)


@pytest.fixture
async def client(settings):
    """Injected-settings client; pooled connections closed after each test."""
    client = TiresClient(settings)
    yield client
    await client.aclose()


@pytest.fixture
async def make_client(settings):
    """Factory for extra clients (custom settings/transport); all closed after."""
    made: list[TiresClient] = []

    def _make(
        settings_override: TiresSettings | None = None, **kwargs: Any
    ) -> TiresClient:
        c = TiresClient(settings_override or settings, **kwargs)
        made.append(c)
        return c

    yield _make
    for c in made:
        await c.aclose()


@pytest.fixture
async def mcp_server(client):
    """A real FastMCP server wired to the injected test client."""
    return create_server(client=client)


@pytest.fixture
async def call_tool(mcp_server):
    """Call an MCP tool through the registered interface and return its payload."""

    async def _call(name: str, arguments: dict | None = None) -> Any:
        result = await mcp_server.call_tool(name, arguments or {})
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    return _call
