# Independent TiresVote MCP

Accepted by the product owner on 2026-09-27. TiresVote is an additional product
that needs independent installation, development and releases. We are creating
a separate `tiresvote_mcp` repository and a `tiresvote-mcp` package instead of
adding a tire catalog to `wheel-size-mcp`; the first release includes the 12
tools from the inventory.

Splitting only by Python modules inside the previous server would have kept a
shared tool list, configuration and release cycle. Therefore the processes are
independent, and the shared user scenario is performed by an agent that has
connected both MCPs.

Both products use a single `WHEELSIZE_API_KEY`, confirmed by the owner.
This creates no dependency of one server on the other. The small HTTP transport
can be adapted from the existing project while preserving its license notices;
we are not introducing a shared package at this time.
