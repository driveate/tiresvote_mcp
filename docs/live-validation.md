# Live validation of the public Tires API

Snapshot taken 2026-09-27 against production
`https://api.wheel-size.com/v2/tires/` using `scripts/probe_live_contracts.py`
(stdlib GET only). 74 serialized GET requests were recorded, timestamped
15:25:03–15:41:56 UTC; status distribution: 63×200, 7×400, 2×403, 2×404.
Requests were serial with a fixed delay. The shared `WHEELSIZE_API_KEY` was
loaded from the local MCP configuration into the process environment only —
it is not present in scripts, logs, docs or fixtures. Sanitized raw responses
are in `.git/coordination/live-fixtures.json` (not versioned); conclusions are
summarized here and reflected in [api-knowledge.md](api-knowledge.md).

## Authentication

| Case | Result |
|---|---|
| No `user_key` | 403 `text/plain` — `Authentication parameters missing` |
| Invalid `user_key` | 403 `text/plain` — `Authentication failed` |
| Valid key | 200 JSON on all 12 inventory paths |

The three auth cases are exactly one call each by design.

## Response envelope and headers

- Successful reports: `{data, meta}`. `meta` contents differ by endpoint:
  `count` (brands, brand catalog, modes, materials, regions, categories),
  `pagination` (search, tests), `last_update` (product detail), `{}` (bnb,
  test detail — empty object, not missing).
- `Cache-Control` observed per endpoint family: `public, max-age=1799,
  must-revalidate` on advanced search and brand catalog; `max-age=1800` on
  modes; `max-age=3600` on product detail, materials and test detail;
  `max-age=7200` on the brands list, BNB and tests list; `max-age=86400` on
  regions and performance-categories; `no-store` on serializer-validation
  400s; auth failures (403), 404s and two other 400s carried no
  `Cache-Control` header at all.
- `meta.pagination` keys: `current_page_count`, `total_items`, `total_pages`,
  `first`, `prev`, `next`, `last` — absolute `api.wheel-size.com` URLs that
  do **not** contain `user_key`. Defense redaction stays in place anyway.

## rf / np / oe semantics — resolved live

Methodology from the previous revision of `api-knowledge.md`: a narrow slice
with pre-registered control slugs, param absent / `true` / `false`, and every
result page enumerated (all slices fit the page budget; no truncation).

Control slice: `b=michelin&s=summer`, `per_page=20`, default ordering.
Controls fixed before trials from `/catalog/michelin/`:

- runflat-flagged summer models (`is_runflat=true`): `pilot-sport-ps2-zp`,
  `latitude-sport-3-zp`, `primacy-3-zp`, `pilot-super-sport-zero-pressure`
- normal models: `pilot-sport-4`, `pilot-sport-5`, `primacy-4`, …
- discontinued summer models (added by `show_discontinued`): `energy-saver`,
  `xas`, `primacy-hp-rrbl`, `pilot-sport-cup`, … (15 in the slice)
- the only Michelin OE model found: `energy-lx4` (season `all`, year 2007,
  `is_oe_model=true` verified via its detail, also discontinued)

| Slice variant | items | Set difference vs baseline |
|---|---|---|
| absent | 34 | — (runflat models, discontinued and OE all excluded) |
| `rf=true` | 38 | + exactly the 4 runflat models |
| `rf=false` | 38 | same set as `rf=true` |
| `np=true` | 49 | + the 15 discontinued summer models |
| `np=false` | 34 | identical slug set to absent |
| `oe=true` | 34 | identical to absent (no non-discontinued OE in slice) |
| `oe=false` | 34 | identical to absent |
| `np=true` on `b=michelin&y=2007` | 10 | — |
| `np=true&oe=true` same slice | 11 | + `energy-lx4` only |

Server-observed mechanics: `meta.pagination.next` URLs expose the rewritten
parameter values — `rf=true` becomes `rf=all`, `np=true` → `np=all`,
`oe=true` → `oe=all`, `false` → `F`. This matches the backend: the DRF
`MappedBooleanField` maps `true→'all'` (skip filter = include) and
`false→'F'` for `np`/`oe`/`rf`, while defaults `['F']` apply when a param is
absent. Literal `rf=all` is rejected: 400 `{"rf": ["Must be a valid
boolean."]}` — the `all` vocabulary is internal, not client-facing.

Conclusions for the MCP contract (public parameter names are fixed in
[tools-inventory.md](tools-inventory.md)):

- **`np`/`oe` are include flags**: `true` adds discontinued/OE models,
  `false` equals omission. Public names `include_discontinued`, `include_oe`
  are evidence-backed.
- **`rf` is asymmetric and must not be named "exclude/include" naively**:
  absent → runflat-flagged models are excluded; `true` → included alongside
  normal models; `false` → also includes runflat models (backend matches
  "model flag false OR has a non-runflat variant"). The finalized public name
  is `runflat_filter` with the documented caveat that `false` does not mean
  exclusion. For "only runflat" filtering use catalog `runflat=true`.
- The Swagger claim "`rf=true` matches a tire when the model itself or any of
  its modifications is runflat" is disproven for `/search/advanced/` — the
  response under `rf=true` is a strict superset of the default and includes
  non-runflat models.

## Catalog `/catalog/{brand}/` semantics

- Defaults: runflat models are **included** by default (validated `rf='all'`),
  discontinued and OE models are excluded — a different default from
  `/search/advanced/`.
- `runflat=true` → restricts to runflat models: Michelin 13, Pirelli 12,
  Bridgestone 20 items, all with `is_runflat=true`. Whether models that are
  runflat only via variant-level flags (backend ORs `modes.runflat`) can
  appear was **not observed**: mode rows expose no runflat flag of their own
  (`modes-*` records carry no such field), so the inference comes from the
  complete `runflat=true` result sets — every model returned there was
  model-flagged. The catalog-level control `pilot-sport-3-zp-2` (ZP-named,
  `is_runflat=false` on its detail) likewise surfaced no variant flag.
- `show_discontinued=true` / `show_oe=true` add their groups (Michelin
  99 → 147 / → 148).
- **200-item cap**: Bridgestone with `show_discontinued=true&show_oe=true`
  returns `meta.count=318` with exactly 200 items — `truncated` reporting is
  required for `tires_list_brand_tires`.
- Unknown brand slug → **200** `{data: [], meta: {count: 0}}`, not 404
  (unknown product/test slugs do return 404 `{"detail": "Not found."}`).

## Search and pagination edges

- `/search/` (`query=primacy hp`) returns discontinued models — the np/oe/rf
  defaults do not apply to query search; it has no such parameters.
  `has_modes` key is present but `{}` when `t` is not used.
- `page=99` on a 4-page result → 200 with `data: []`, `current_page_count: 0`
  and API-hosted pagination links; the documented HTML hand-off to
  tiresvote.com did **not** trigger (cap setting presumably disabled).
- `per_page=21` → 400 `{"per_page": ["Ensure this value is less than or equal
  to 20."]}`; `ordering=bogus-field` → 400 `{"ordering": ["Undefined sort
  field bogus-field ..."]}`; missing `query` → 400 `{"query": ["This field is
  required."]}`; `year=abc` on tests → 400 `{"year": ["A valid integer is
  required."]}`; `has_mode` >30 chars → 400 `{"has_mode": ["Ensure this field
  has no more than 30 characters."]}`.

## Observed field shapes worth pinning in contract tests

- `price_segment` is a display string (`"Premium"`, `"Mid-Range"`,
  `"Economy"`); filtering `price_segment=premium` returned exactly the 6
  premium brands (distribution 6/37/144 over 187 brands).
- `rim_diameter` serializes as float (`16.0`); `dual_load_index` is null on
  regular metric modes and populated on LT-metric ones — one coherent
  recorded row (`modes-km3`): `215/75 R15 100/97Q` with `load_index=100`,
  `dual_load_index=97`, `rim_diameter=15.0`, `sizing_system=lt-metric`.
  Observed `sizing_system` values: `metric`, `lt-metric`; the flotation
  branch (`overall_diameter`, `section_width`) exists in the serializer but
  was not found live on michelin/bfgoodrich/mickey-thompson samples.
- `counters.modes` returned `0` for `pilot-sport-4` (list, detail and search
  rows) while `/modes/` reports `meta.count=150` — never infer variant
  presence from the counter; use the modes endpoint.
- `has_modes` in advanced-search rows maps each requested `t` to the list of
  matching variant notations, `null` for a requested size that did not match
  (`primacy-4plus` → `"315/35R20": null`), `{}` when `t` was not sent.
- BNB: `data: null` with empty `meta: {}` for a model without approved
  reasons (`pilot-sport-3-zp-2`); populated models return
  `{buy: [...], not_buy: [...]}` items as `{text, prooflink, upvotes}`.
- Materials: `type` filter works; observed `article` (`title`, `tags_list`,
  `lead`, `image`, `canonical_link`), `video` (`video_url`, `thumbnail`,
  `title`), `benchmark` (`title`, `season`, `automobile_type`,
  `canonical_link`, `product_rank{place, description, positive_tags,
  negative_tags}`); `type=link` is accepted but returned an empty list on the
  probed product.
- Test detail (`/tests/{slug}/`): `items[]` carry `place` (1-based),
  `description`, `positive_tags`/`negative_tags` (strings), `test_score`
  (decimal, e.g. `2.2`), `recommend` (bool) and `product` whose `has_modes`
  maps each `has_mode` parameter to matching sizes or `null` — an annotation
  of participant sizes, not a test-size filter.
- `regions`: 20 entries, `tree_level` 1–3, `countries` populated on deeper
  levels only (e.g. `usdm` lists its countries).
- `performance-categories`: 26 entries; nested `season`/`automobile_type`
  objects can be `{slug: null, display: null}`.
- Tests list: 366 items; `year` filter works (`year=2026` → 31 items). The
  `year` field does not necessarily match the year encoded in the slug —
  e.g. slug `2027-autoview-all-season-tire-test-r19` carries `year: 2026`.
  Whether `year` is a season year or publication year is not stated by the
  API; preserve the field as returned rather than interpreting it.

## Not proven / remaining gaps

- Flotation (`overall_diameter`/`section_width`) mode serialization — no live
  sample among probed products.
- Variant-level (mode-only) runflat contribution to catalog `runflat=true` —
  no such model appeared in the three complete `runflat=true` result sets
  probed (every returned model carried `is_runflat=true` at model level, and
  mode rows expose no runflat flag to inspect directly).
- `WS_API_SEARCH_MAX_PAGE_NUMBER` / HTML-boundary behavior — did not engage.
- 429 / rate-limit behavior — intentionally not probed.
- Whether every response field type is stable under other data (e.g.
  `Counters.modes` is declared a string in the schema but arrived as int).
- Small residual risk: observed behavior matches the PostgreSQL search path
  of checkout `9a461a0`; parity on earlier deployments is not asserted.

## Related verification

This page records **upstream contract** evidence only — 74 serialized GET
probes plus one independent catalog GET cross-check. The MCP-level checks
that consume this contract (offline response replay through the registered
server, wheel install, stdio handshake and the opt-in 12-GET live smoke)
are recorded in [validation.md](validation.md). Across the whole task,
87 real production GETs were made; the 63-response replay is fully offline.
