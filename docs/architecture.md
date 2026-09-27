# First-version architecture

This describes the implemented v0.1.0 architecture. The product decision is
recorded in [ADR-0001](adr/0001-independent-mcp.md), the tool list in the
[inventory](tools-inventory.md), and production evidence in
[live-validation.md](live-validation.md).

## Structure

```text
src/tiresvote_mcp/
  __init__.py
  server.py             # FastMCP, registration, instructions, CLI, config://status
  client.py             # HTTP, configuration, retry, errors, secret redaction
  response.py           # Compact projections and two pagination kinds
  prompts.py            # Four standalone TiresVote scenarios
  tools/
    __init__.py
    catalog.py          # Brands, models, variants, regions, categories (6)
    search.py           # Text and parametric search (2)
    evidence.py         # Reasons, materials, test list and details (4)
tests/
  fixtures/             # Redacted contract examples without keys
```

Python >=3.12, FastMCP and httpx; uv for dependencies (committed `uv.lock`),
pytest/pytest-asyncio/respx for tests, Ruff for checks.

Each tool module exposes `register(mcp)`. Parameters are typed via
`Annotated`/`Field`; the docstring explains purpose, preconditions and limits.
Tests go through the registered MCP interface. The client allows substituting
the HTTP transport and explicit settings without importing neighboring projects.

## Configuration

| Variable | Purpose / default |
|---|---|
| `WHEELSIZE_API_KEY` | Owner's key, shared with Fitment; sent as `user_key` |
| `TIRES_API_BASE_URL` | `https://api.wheel-size.com`, without `/v2/tires/` |
| `TIRES_API_HOST_HEADER` | Empty; override only for explicitly configured local routing |

The `/v2/tires/…` path is set by the tool. A missing key is allowed during
server registration and in offline tests; a refusal from the public gateway
turns into a clear configuration error. Status reports only the presence of
the key, the version and the safe API address. The key value is never returned.

The first version's primary transport is stdio. HTTP transport and remote
hosting can be added as a separate task; they are not a first-release
requirement.

## Separation of responsibilities

- The HTTP module owns retry, statuses and DRF errors; hints refer only to
  TiresVote tools. A custom Host is passed for local testing.
- Tool modules translate plain MCP parameters into upstream parameters and
  choose the response projection. Validation errors do not trigger a hidden
  catalog-wide sweep.
- The response module preserves identifiers, links, null semantics and
  truncation details. Pagination policies are described in the knowledge file.
- The product does not depend on Django, the TiresVote DB, the `ws_mcp` package
  or neighboring checkouts.
- An extra persistent cache and a shared library for the two MCPs are not
  needed for v1.

## MCP prompt scenarios

| Prompt | Input | Result |
|---|---|---|
| `tire_selection_by_size` | Size, season, market, preferences | Shortlist of models with verified variants and rationale |
| `tire_comparison` | 2–4 models, size and criteria | Comparison table with sources and data gaps |
| `tire_test_explainer` | Test name/slug, desired size | Test results plus the requested size's availability among participants |
| `tire_model_brief` | Brand and model | Characteristics, variants, family, reasons and materials |

Server instructions carry a brief chain selection for clients without prompts
support. Search resolves identifiers, the model card and sizes verify the
candidate, materials/tests justify the conclusion. Each scenario bounds the
number of candidates and requests; additional pages are fetched as the task
requires.

If a separate Wheel-Size MCP is connected, the agent can fetch fitment first.
Internal calls into another MCP and a hard dependency on its presence are not
needed.

## Description and publication

All tools are read-only. Tags reflect catalog/search/evidence. The usage rules
for the Tires API specifically must be confirmed separately: Fitment rules must
not be declared an established Tires contract. Until clarified, scenarios assume
a user-initiated request and a bounded sample, without background bulk
collection.

Public descriptions explicitly distinguish catalog, evidence and fitment.
Publishing manifests, release CI and remote HTTP deployment remain follow-up
tasks outside the local v0.1.0 scope.
