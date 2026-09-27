"""Scoped client binding for tool-module registration.

Tool modules expose ``register(mcp, client=None)``. ``create_server()`` calls
them inside :func:`bind_client`, so a plain ``register(mcp)`` resolves the same
injectable client; a ``RuntimeError`` is raised when neither path supplies one.
No module-level env-bound client exists.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tiresvote_mcp.client import TiresClient

_bound_client: ContextVar["TiresClient | None"] = ContextVar("tiresvote_tools_client", default=None)


@contextmanager
def bind_client(client: "TiresClient") -> "Iterator[None]":
    """Bind ``client`` as the registration-scope default for ``register(mcp)``."""
    token = _bound_client.set(client)
    try:
        yield
    finally:
        _bound_client.reset(token)


def require_client(explicit: "TiresClient | None") -> "TiresClient":
    """Resolve the client for a ``register()`` call: explicit arg wins."""
    if explicit is not None:
        return explicit
    client = _bound_client.get()
    if client is None:
        raise RuntimeError(
            "register() needs a TiresClient: pass register(mcp, client) or call "
            "inside tiresvote_mcp.server.create_server()."
        )
    return client
