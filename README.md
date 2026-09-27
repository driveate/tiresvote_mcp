# tiresvote-mcp

Independent MCP server for the TiresVote tire catalog and professional tire
tests, served over stdio via FastMCP. Read-only access to the public Tires
API (`https://api.wheel-size.com/v2/tires/`).

**Status: v0.1.0 working local release.** 12 tools, four workflow prompts,
a status resource, an offline test suite, an opt-in 12-request live smoke
suite and a wheel build — all verified; the reproducible record is in
[docs/validation.md](docs/validation.md). This is a local package: it is
not published to PyPI and has no remote deployment.

## Product

The server helps an agent find a tire model, check its known size variants,
compare models and collect professional test results with citations.

TiresVote MCP is developed independently from `wheel-size-mcp`: its own
repository, package, process and releases. Vehicle-to-tire compatibility
(fitment) remains a Wheel Fitment API concern — this server never answers
fitment questions. Both MCP servers can be connected at once; the user's
agent composes them.

## Tools

Exactly 12 read-only tools, all prefixed `tires_`:

| Group | Tools |
|---|---|
| Catalog | `tires_list_brands`, `tires_list_brand_tires`, `tires_get_tire`, `tires_list_sizes`, `tires_list_regions`, `tires_list_performance_categories` |
| Search | `tires_search`, `tires_search_advanced` |
| Evidence | `tires_get_pros_cons`, `tires_list_materials`, `tires_list_tests`, `tires_get_test` |

Parameters, projections and upstream mapping:
[docs/tools-inventory.md](docs/tools-inventory.md).

Responses are compact bounded projections — every tool response is capped
at 40,000 serialized bytes (the ~8k-token per-response budget is an
approximate target, not a hard guarantee) and reports `has_more`/
`next_offset`/`next_page`/`truncated` explicitly so an agent can page
without walking the whole catalog. Nested collections are cut along
explicit navigation routes; reference lists keep categories and countries
whole under the byte guard. A single oversized object with no continuation
(for example one reference entry at `limit=1`) yields a bounded, actionable
error instead of a silent cut.

## Prompts

Four workflow prompts, rendered as bounded plans the agent executes with
the tools above:

- `tire_selection_by_size` — shortlist models with catalog-listed variants
  in a given size (catalog presence, never a stock claim).
- `tire_comparison` — compare 2–4 models on published data.
- `tire_test_explainer` — explain a professional test, relating results to a
  requested size without mixing them.
- `tire_model_brief` — compile a cited dossier on one model.

One resource: `config://status` — a safe configuration snapshot (key
presence only, never the key).

Not in scope for v1: the shared article catalog, third-party top charts,
standalone user reviews, the Editorial API, current pricing/stock, data
mutations and a custom rating algorithm.

## Installation

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/). From a source
checkout:

```sh
uv sync --dev          # installs the package plus dev tools
uv run tiresvote-mcp   # serves MCP over stdio
```

`uv sync` does not put an unqualified `tiresvote-mcp` on your global PATH —
it lives in the project `.venv`. For MCP client configuration use an
absolute invocation. The uv path below is an example — replace it with the
absolute path of the `uv` executable on your machine (`which uv`), and the
project path with your checkout:

```json
{
  "mcpServers": {
    "tiresvote": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory", "/absolute/path/to/tiresvote_mcp",
        "run", "--no-sync", "tiresvote-mcp"
      ],
      "env": { "WHEELSIZE_API_KEY": "<your key>" }
    }
  }
}
```

or the installed entry point directly:

```json
{
  "mcpServers": {
    "tiresvote": {
      "command": "/absolute/path/to/tiresvote_mcp/.venv/bin/tiresvote-mcp",
      "env": { "WHEELSIZE_API_KEY": "<your key>" }
    }
  }
}
```

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `WHEELSIZE_API_KEY` | yes (for real calls) | Tires/Wheel Fitment API key — the same key works for both APIs; sent upstream as the `user_key` query parameter |
| `TIRES_API_BASE_URL` | no | API origin override, default `https://api.wheel-size.com`. Origin only — no path, query or credentials |
| `TIRES_API_HOST_HEADER` | no | Explicit `Host` header for local routing/gateways |

The server reads the **process environment only** — `.env` files are not
auto-loaded; [.env.example](.env.example) documents the variables but is not
a config mechanism. Set the key in the MCP client's server `env` block (as
above) or in the process environment.

The API key is never echoed back: it is redacted from surfaced URLs, error
messages, logs and the `config://status` resource.

## Running

From the source checkout use `uv run`; a bare `tiresvote-mcp` works only in
a shell/venv where the package's console script is explicitly installed or
activated:

```sh
uv run tiresvote-mcp                    # serves MCP over stdio
uv run tiresvote-mcp --version          # prints the package version
uv run tiresvote-mcp --transport stdio  # stdio is the only transport in v1
uv run python -m tiresvote_mcp          # equivalent entry point
```

## Development and verification

```sh
uv sync --dev
uv run ruff check .
uv run pytest -m "not integration"   # offline suite; no network
uv build                             # wheel + sdist in dist/
```

Offline tests never touch the network: HTTP is mocked (respx/MockTransport)
and a socket/DNS-level guard fails any real egress attempt.

A stdio handshake checker ships in `scripts/check_stdio.py` — the server
command goes after `--`, and `--timeout SECONDS` bounds each response wait:

```sh
# source checkout
uv run --no-sync python scripts/check_stdio.py -- python -m tiresvote_mcp
# installed wheel
python3 scripts/check_stdio.py -- tiresvote-mcp
```

### Opt-in live smoke

`tests/test_integration.py` exercises all 12 tools and the surface
(tools/list, prompts/list, `config://status`) against production — 12 GETs
at most, `max_retries=0`, tiny limits. It runs ONLY when both the
`--run-live` flag and the `WHEELSIZE_API_KEY` environment variable are
present; the default suite stays offline even if the key is set:

```sh
WHEELSIZE_API_KEY='<your key>' uv run pytest -m integration --run-live
```

This smoke passed on 2026-09-27 (2 tests, 12 GETs) — details and totals in
[docs/validation.md](docs/validation.md). Upstream contract evidence (74
serialized GET probes on all 12 endpoints) is in
[docs/live-validation.md](docs/live-validation.md); the bounded probe
helper is [scripts/probe_live_contracts.py](scripts/probe_live_contracts.py).

### Known gaps

- Rate-limit (429) behavior was intentionally not probed upstream.
- No live sample of flotation-size serialization (`overall_diameter`/
  `section_width` mode fields) has been found yet.
- Variant-level (mode-only) RunFlat contribution to catalog `runflat=true`
  is implemented upstream but unobserved live.

## Documentation

- [AGENTS.md](AGENTS.md) — where an agent should start and which docs to read.
- [CONTEXT.md](CONTEXT.md) — domain terminology.
- [Architecture](docs/architecture.md) — modules, configuration and workflows.
- [Tool contracts](docs/tools-inventory.md) — the 12 tools and parameter mapping.
- [API knowledge](docs/api-knowledge.md) — formats, limits, errors and data provenance.
- [Validation record](docs/validation.md) — reproducible checks and outcomes.
- [Live validation](docs/live-validation.md) — recorded production evidence.
- [Scenario checks](docs/scenarios.md) — manual tool-choice evaluation.
- [Implementation plan](docs/implementation-plan.md) — stages and acceptance criteria.
- [Separation ADR](docs/adr/0001-independent-mcp.md) — why this is a separate product.
- [Swagger snapshot](docs/reference/README.md) — offline schema reference.

## API

Public base URL: `https://api.wheel-size.com/v2/tires/` —
[Swagger](https://api.wheel-size.com/v2/tires/swagger/). The key is sent
upstream as the `user_key` query parameter; it must not appear in MCP
responses, logs or stored fixtures.

## License

MIT — see [LICENSE](LICENSE).
