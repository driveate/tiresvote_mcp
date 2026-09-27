# Validation record — v0.1.0

Verification of the local `tiresvote-mcp` release on Python 3.12.0,
2026-09-27. Development commands run from a source checkout with `uv`;
the installed-wheel check runs outside the checkout.

## Offline

```sh
uv sync --dev                          # resolve + install dev group
uv run ruff check .                    # lint
uv run pytest -m "not integration"     # offline suite — no network
uv build                               # wheel + sdist in dist/
```

- `uv sync --dev`, `uv run ruff check .` and `uv build` all passed.
- Offline suite: **224 passed, 2 deselected** (the deselected pair is the
  opt-in live smoke). HTTP is mocked (respx/MockTransport); a socket/DNS
  guard fails any real egress.
- Live gate verified: `uv run pytest -m integration` without `--run-live`
  skips both live tests with **zero network requests**, even when
  `WHEELSIZE_API_KEY` is set in the environment.
- Schema/prompt checks cover the registered tool names, argument metadata,
  English descriptions and prompt safety rules; the eight tool-choice
  cases in [scenarios.md](scenarios.md) are a manual rubric — no paid LLM
  evaluation was run.

## Contract replay (offline)

The 63 successful sanitized responses recorded during live probing were
replayed through the registered FastMCP `Client` with a `MockTransport` —
zero network. 63/63 passed; the largest real-example tool response was
17,800 serialized bytes, under the 40,000-byte per-tool ceiling
(`MAX_RESPONSE_BYTES` in `src/tiresvote_mcp/projections.py`; the ~8k-token
per-response budget is an approximate target, not a hard guarantee).

## Wheel install + stdio handshake

The wheel built by `uv build` was installed into an independent venv from
the built artifact (locked runtime dependencies, no editable source).
From outside the checkout:

```sh
/path/to/venv/bin/python /path/to/checkout/scripts/check_stdio.py -- \
  /path/to/venv/bin/tiresvote-mcp
```

All 9 protocol checks passed: version 0.1.0, 12 tools, 4 prompts, the
`config://status` resource, one mocked brands call, stdout carrying
JSON-RPC only, and a synthetic key absent from output. `check_stdio.py`
takes the server command after `--` (`--timeout SECONDS` bounds each
response wait).

## Live MCP smoke (opt-in)

Run 2026-09-27T16:25:34 UTC against production with the shared key injected
from the environment — never stored or printed:

```sh
WHEELSIZE_API_KEY='<your key>' uv run pytest -m integration --run-live -q
```

`2 passed`, 12 GETs total — one per tool at `max_retries=0` — covering all
12 tools, the 4 prompts and the status resource. The suite is opt-in: it
runs only when both `--run-live` and `WHEELSIZE_API_KEY` are present, and
the default suite stays offline even when the key is set.

## Production traffic totals

- 74 serialized GET probes on all 12 endpoints — upstream contract evidence
  in [live-validation.md](live-validation.md).
- 1 independent catalog GET (brands count cross-check).
- 12 GETs by the MCP smoke above.

87 real production GETs in total. The 63-response replay is fully offline
and does not add to that count.

## Known live gaps

- Rate-limit (429) behavior intentionally not probed upstream.
- No live sample of flotation-size serialization (`overall_diameter`/
  `section_width` mode fields) found.
- Variant-level (mode-only) RunFlat contribution to catalog `runflat=true`
  is implemented upstream but unobserved live.
- The `WS_API_SEARCH_MAX_PAGE_NUMBER` HTML hand-off boundary was verified
  offline only.

## Repository audit

Public documentation, descriptions and code comments are in English.
Final scans found no stored API key or broken local links. The OpenAPI snapshot
is unchanged (sha256 `6d1aef4e1bb09d96bb81ebc55b2543cce534dfc604c02a72dcb56772e3e56e22`).
