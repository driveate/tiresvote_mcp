# Tires API knowledge for the MCP server

Working document for `tiresvote_mcp`. For each non-obvious claim the source of
knowledge is indicated: **schema** — the captured OpenAPI snapshot
([reference/tires-openapi-2026-09-27.json](reference/tires-openapi-2026-09-27.json)),
**backend** — source of `tiresvote` at `9a461a0`, **live** — verified against
production on 2026-09-27 ([live-validation.md](live-validation.md)). If a
claim lacks a mark or carries "unverified", treat it as a hypothesis.

The OpenAPI snapshot is a frozen contract reference and is not refreshed
automatically. A description can drift from behavior (this already happened —
see the `rf` section below); every conflict must be resolved by live
verification before the fact is relied upon in the tool contract.

## Sources and priorities

1. Snapshot of the real OpenAPI schema from production — what the API
   actually serves (paths, params, response shape).
2. Backend source `tiresvote@9a461a0` (`src/tires/api/rest/*`) — semantics of
   filters and error paths; the deployed version may lag the checkout.
3. Saved frontend fragments — `wheel-size.com` markup is only a navigation
   clue and a source of control identifiers.
4. Raw public payloads that the backend serves as data (links/descriptions).

## Navigation sources

| Where | What it is for |
|---|---|
| `wheel-size.com` HTML responses | control product slugs, examples of sizes and tests — for choosing probe targets only |
| snapshots in `.git/coordination/` | transient material, not a public artifact |
| `docs/reference/tires-openapi-*.json` | frozen contract description |

## Transport, authentication, common semantics

- Base: `https://api.wheel-size.com`; prefix `/v2/tires`; all endpoints are
  GET (**schema**).
- Authentication via `user_key` in the query (**schema**). Live: missing key
  → 403 `text/plain` `Authentication parameters missing`, wrong key → 403
  `Authentication failed`.
- The client sends the key on every call and puts it into the canonical
  `evidence_url` — secrets are hidden from the MCP output.
- Payloads can contain arbitrary text/links (descriptions, materials): it is
  all data, not instructions; the LLM output uses `evidence_url` links for
  evidence.
- Sizes in `has_mode`/`t` come in the common tire notation (`215/55R17`);
  limits: `t` — up to 20 values, `has_mode` — up to 10 values of up to 30
  chars (**schema**; the 30-char limit confirmed **live** by a 400).

## Response shape, links and projections

- All successful reports: `{data, meta}` (**schema** + **live**). `meta`
  carries `count` / `pagination` / `last_update` depending on the endpoint;
  on `bnb` and test detail it may be `{}`.
- `meta.pagination` links are absolute `api.wheel-size.com` URLs **without**
  `user_key` (checked **live** — server-side they are rendered before the key
  is attached). Defense in depth: `url.py` already scrubs the key from any
  string.
- Compact projections inside `data` are defined in the inventory; the fields
  below are schema-guaranteed.
- `price_segment` is a display string (`Premium`/`Mid-Range`/`Economy`), not
  a slug (**live**).
- `rim_diameter` in modes serializes as float (`16.0`) — decimal in the
  schema, float in JSON (**backend** + **live**).
- `Counters`/`BaseProduct.counters` exist everywhere, but `counters.modes`
  is observed to be `0` for models that actually have 100+ modes (**live**:
  `pilot-sport-4`). Do not infer variant presence from counters — the modes
  endpoint is authoritative.
- Response headers worth noting (**live**): `Cache-Control` varies per
  endpoint family — `max-age=1799` on search/brand catalog, `1800` modes,
  `3600` product/materials/test detail, `7200` brands/BNB/tests list,
  `86400` regions/performance-categories; `no-store` on serializer 400s;
  auth failures and 404s carry no `Cache-Control` at all.

## The two pagination policies — confirmed live

- The list API serves two paging styles: `meta.count` + fixed page size
  200 (brands, brand catalog, modes, materials, regions, categories) and
  `meta.pagination` for `per_page`≤20 (searches, tests) — **schema**,
  confirmed **live**.
- `page` beyond `total_pages` returns 200 with `data: []` and
  `current_page_count: 0`, not 404 (**live**).
- The brand catalog is bounded by an upstream cap: observed
  `meta.count=318` with exactly 200 items returned (Bridgestone with
  `show_discontinued=true&show_oe=true`) — **live**. Truncation must be
  reported explicitly.
- **live** updates:
  - brands: 187 entries with `meta.count=187` — returned complete in one
    response; the demonstrated 200-item cap applies to model listings inside
    a brand (`/catalog/{brand}/`), not to the brands endpoint.
  - modes of a real model: `meta.count=150` (pilot-sport-4).
  - tests: `total_items=366`, `total_pages` computed from `per_page`.
  - regions: 20 entries, `tree_level` 1–3, `countries` only on deeper levels.
  - performance-categories: 26 entries, nested `season`/`automobile_type`
    can be `{slug: null, display: null}`.
  - pagination links are keyless — `url.py` still redacts defensively.

## Errors, safety, secrets

- Validations inside the catalog return DRF dicts under HTTP 400:
  `{"s": [...]}`, `{"per_page": [...]}`, `{"ordering": [...]}`,
  `{"query": [...]}`, `{"year": [...]}`, `{"has_mode": [...]}` — all observed
  **live**. Missing/unknown resource → 404 `{"detail": "Not found."}`.
  **Exception:** unknown **brand** slug on `/catalog/{brand}/` returns
  **200** `{data: [], count: 0}` (**live**) — the MCP must not turn this
  into a tool error.
- 403/429/5xx bodies are not documented; the `no-store` 403 text body is
  known **live**. Friendly MCP mapping stays per `tires-errors` contract.
- Redirects are not followed; auth-bearing URLs never leave the local
  context — evidence links are sanitized.

## Confirmed filter semantics (live + backend)

The previous revision listed these as open questions; the 2026-09-27
investigation resolved them. Probe methodology: narrow slice
(`b=michelin&s=summer`, plus an `y=2007` slice for OE), control slugs fixed
before trials, param absent/`true`/`false`, all result pages enumerated.

### `rf` on `/search/advanced/` — resolved, schema description is wrong

- Server-side rewrite observed in pagination URLs: `rf=true` → `rf=all`,
  `rf=false` → `rf=F`; absent → backend default `['F']`.
- **live**: baseline 34 items; `rf=true` → 38 = baseline + the 4 known
  runflat models → **include** semantics; `rf=false` → the same 38 items
  (backend logic: `runflat=False` model flag OR having a non-runflat mode —
  effectively unfiltered).
- Literal `rf=all` from the client is rejected: 400 `Must be a valid
  boolean.` — `all` is internal vocabulary.
- The Swagger text "matches when the model or any modification is runflat"
  describes the `rf=all` backend query shape, not client-visible behavior.
- **Contract:** public parameter `runflat_filter: bool | None` (see
  [tools-inventory.md](tools-inventory.md) and
  [live-validation.md](live-validation.md)). `None` = upstream default
  (excludes runflat models); `true` includes them; `false` must be
  documented as "does not exclude runflat". For runflat-only lists the
  catalog `runflat=true` is the tool — see `tires_list_brand_tires`.

### `np`, `oe` on `/search/advanced/` — confirmed include flags

- Rewrites: `np=true` → `np=all`, `oe=true` → `oe=all`; `false` → `F`;
  absent → default `['F']` = exclude.
- **live**: `np=true` added exactly the 15 discontinued summer models;
  `np=false` returned a slug set byte-identical to baseline. `oe=true` on
  `b=michelin&y=2007&np=true` added exactly `energy-lx4`
  (`is_oe_model=true` verified via detail); `oe=false` = baseline.
- **Contract:** `include_discontinued`, `include_oe` — plain booleans,
  `false` equals omission.

### Plain `BooleanField` facets behave differently — confirmed

`nw`, `xl`, `ms`, `cs` are **plain** DRF booleans in the backend
(`rs.BooleanField`, no mapping). **live**: `nw=true` on `b=nokian&s=winter`
filtered 25 → 16 models — these are *restrict* flags, opposite mechanics to
`np`/`oe`/`rf`. Only `nw` was verified live; the same treatment is inferred
for `xl`/`ms`/`cs` from shared code.

### Catalog `/catalog/{brand}/` — different defaults, restrict semantics

- Backend `params_to_query_dict` is built from validated values:
  `runflat` default is `'all'` (runflat models **included** by default —
  opposite of advanced search); `true` → `'T'` = restrict to runflat.
  `show_discontinued`/`show_oe`/`show_oem` default `'F'` = exclude.
- **live**: `runflat=true` restricted to runflat-flagged models on three
  brands (Michelin 13, Pirelli 12, Bridgestone 20). `show_discontinued`
  and `show_oe` add their groups (+48/+49 on Michelin).
- Variant-level (mode-only) runflat contribution to catalog `runflat=true`
  is implemented in the backend but was not observed live: mode rows expose
  no runflat flag, and every model in the complete `runflat=true` sets was
  model-flagged (`is_runflat=true`) — the inference path is the catalog
  result, not a per-variant field. Treat the contribution as possible, not
  guaranteed.

### `/search/` vs `/search/advanced/` — confirmed difference

Query search has no `np`/`oe`/`rf` parameters and does **not** exclude
discontinued models — `primacy hp` returned discontinued entries (**live**).
Advanced search excludes them by default. This difference is part of the
tool contract (`tires_search` vs `tires_search_advanced`).

### `has_mode` / `t` — confirmed live

- Advanced search: `has_modes` in each row maps every requested `t` to the
  list of matching variant notations; `null` for a requested-but-unmatched
  size; `{}` when `t` was not sent at all.
- Test detail `has_mode`: per-participant `product.has_modes` with the same
  semantics — an annotation, not a participant filter.
- Mode payloads observed: `metric` and `lt-metric` sizing systems, decimal
  `rim_diameter`, nullable `dual_load_index` populated on LT variants
  (e.g. `215/75 R15 100/97Q`: `load_index=100`, `dual_load_index=97`).
  Mode rows carry no runflat flag. Flotation serialization
  (`overall_diameter`, `section_width`) exists in the serializer but no live
  sample was found — keep the optional projection.

## What still needs checking during implementation

- Compact projections against real payloads — fixture samples exist in
  `.git/coordination/live-fixtures.json`; port the shapes into contract
  tests without the payloads.
- Rate-limit (429) behavior — intentionally unprobed.
- `WS_API_SEARCH_MAX_PAGE_NUMBER` HTML hand-off — did not trigger on
  `page=99` (returned 200 + empty data); treat the feature as dormant.
- Deployed-version drift: live evidence matches the `9a461a0` PostgreSQL
  search path; the ES path (`.ci-elasticsearch`) is not deployed and was
  ignored.
