"""Tool module seam.

Each module exposes ``register(mcp, client=None)``. The client is the
injectable :class:`tiresvote_mcp.client.TiresClient` captured by the
registered tool closures; when omitted, the scoped default bound by
``create_server()`` is used. Modules validate MCP-facing parameters, map them
to upstream API parameters and pick a response projection — they never invent
upstream fields.
"""
