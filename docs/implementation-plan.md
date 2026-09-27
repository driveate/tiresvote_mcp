# Implementation plan

Authoritative scope — [inventory](tools-inventory.md), caveats —
[API knowledge](api-knowledge.md), production evidence —
[live-validation.md](live-validation.md). Work happens in this repository;
neighboring projects are read-only references.

## Status record (2026-09-27)

All six stages are complete for the local v0.1.0 release; the reproducible
evidence for each claim lives in [validation.md](validation.md).

| Stage | State |
|---|---|
| 1. Minimal runnable server | Done — package, `uv.lock`, `tiresvote-mcp` CLI (stdio), `config://status` |
| 2. Client and response handling | Done — GET-only client, redaction, DRF/plain-text errors, retries, both pagination policies |
| 3. Catalog and search | Done — all 6 catalog and 2 search tools registered |
| 4. Evidence and tests | Done — all 4 evidence tools registered; exactly the 12 inventory names |
| 5. Scenarios and documentation | Done — four prompts, scenario rubric in [scenarios.md](scenarios.md), English descriptions |
| 6. First local version check | Done — offline suite, contract replay, wheel build + independent install, stdio handshake and the opt-in 12-GET live smoke all recorded in [validation.md](validation.md) |

Remaining follow-up scope (not part of the local release): PyPI publishing,
registry metadata, release CI and remote HTTP deployment.

## 1. Minimal runnable server

Create pyproject, uv.lock, src layout and the `tiresvote-mcp` CLI; FastMCP server
`tiresvote`, stdio, configuration and a safe `config://status`.
Add a test HTTP transport and the first vertical scenario `tires_list_brands`.

Done when the CLI starts from a fresh checkout after `uv sync --dev`, MCP
discovers the first tool, and calling it on a mock returns a readable response.
README contains a real verified local launch command.

## 2. Client and response handling

Implement the Tires client, redaction, DRF errors, retry and the two pagination
policies. Keep only the needed parts of the old project's transport with its
license; do not carry over the global Fitment environment or `ws_mcp` imports.

Done when offline checks cover query arrays, omitted/false/true, Host override,
400 dict/list, 404, plain-text 401/403, 429/5xx retry, transport error and an
invalid 200. Neither response nor exception contains the test secret.
The last page, an empty list, a >200-model brand and an HTML `next` are
verified.

## 3. Catalog and search

Implement all 6 catalog tools and 2 search tools with parameter descriptions.
Add cards, the model family, sizes, reference lists and projections.

Done when MCP contains the 8 expected tools; the search → get_tire → list_sizes
scenario works via `mcp.call_tool` on mocks. Tests check null relations,
fractional sizes, metric and inch systems, `np/rf/oe` flag passing and proper
JSON Schema for lists. Identifiers/links are not lost. The offline test
confirms only parameter passing: the meaning of `rf` differs between code and
snapshot — resolved live on 2026-09-27 as the neutral `runflat_filter`
(`false` does not exclude RunFlat), recorded in
[API knowledge](api-knowledge.md) and [live-validation.md](live-validation.md).

## 4. Evidence and tests

Implement the 4 evidence tools: pros/cons, materials, list and details of pro
tests. When limiting long BNB lists, define navigation parameters that allow
reaching each side; fix them in the inventory before declaring the stage done.

Done when exactly the 12 names from the inventory are registered and each tool
runs on mocks. Verified: null BNB, all material kinds, participant pagination,
tested size separately from has_modes, distinct score/popularity/test_score.
tools/list must not contain top charts, the general article catalog or a
generic HTTP tool.

## 5. Scenarios and documentation

Add the four prompts from the architecture and brief server instructions.
Finalize the English tool docstrings and Field descriptions, synchronize the
inventory. Add questions that verify tool selection without a mandatory paid
LLM eval.

Checked scenarios: resolving an ambiguous name, selection by size, model
comparison, a dossier with missing data, test size ≠ requested size, a two-size
set, conflicting size requirements and an attempt to get warehouse prices.
Done when scenarios preserve evidence provenance and do not promise data
absent from the public contract. Prompts do not require a Wheel-Size
connection.

## 6. First local version check

From a fresh checkout:

```sh
uv sync --dev
uv run ruff check .
uv run pytest -m "not integration"
uv build
```

Verify the built package launches via stdio and MCP list/call — the
`scripts/check_stdio.py` helper takes the server command after `--` —
including the absence of service logs in the protocol stdout. Tests require
no neighboring checkouts, Django or network. Long-response size is checked
on worst-case fixtures: recommended target budget up to 8,000 tokens per
response (approximate — the enforced bound is a serialized byte cap recorded
in the inventory), with explicit truncation and navigation.

Live tests run separately with configured access and record the environment
used: `tests/test_integration.py` is opt-in via `--run-live` plus
`WHEELSIZE_API_KEY` and makes at most 12 GETs (one per tool). Probe and
fixtures concern the Tires API, not Fitment `/v2/regions/`. A local index
can confirm process availability, but an authenticated catalog smoke is
needed to verify the key through the gateway. Do not log the key or the
full URL.

Result: README with a working launch, 12 tools, 4 prompts, inventory, green
offline checks and a list of actually performed/skipped live checks.
Python package publishing, registry metadata, CI release and deployment are
follow-up tasks, not a readiness condition for the local implementation.
