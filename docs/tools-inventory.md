# Tools inventory — first version

Status: implemented — exactly the 12 `tires_*` tools below are registered.
The registered names, English docstrings and `Field` descriptions are the
authoritative wording; `docs/tool-schemas.json` is a generated snapshot of the
registered input schemas. `tests/test_inventory.py` verifies synchronization
through the registered MCP interface (not by parsing source).

All paths below start with `/v2/tires/` and are invoked via GET.
`brand`, `product`, `slug` are obtained from catalog/search responses; a model
name does not guarantee a match with its slug. Size notation is passed without
slug normalization.

| Tool | Relative API path | Module |
|---|---|---|
| `tires_list_brands` | `/catalog/` | catalog |
| `tires_list_brand_tires` | `/catalog/{brand}/` | catalog |
| `tires_get_tire` | `/catalog/{brand}/{product}/` | catalog |
| `tires_list_sizes` | `/catalog/{brand}/{product}/modes/` | catalog |
| `tires_search` | `/search/` | search |
| `tires_search_advanced` | `/search/advanced/` | search |
| `tires_get_pros_cons` | `/catalog/{brand}/{product}/bnb/` | evidence |
| `tires_list_materials` | `/catalog/{brand}/{product}/materials/` | evidence |
| `tires_list_tests` | `/tests/` | evidence |
| `tires_get_test` | `/tests/{slug}/` | evidence |
| `tires_list_regions` | `/regions/` | catalog |
| `tires_list_performance_categories` | `/performance-categories/` | catalog |

Excluded upstream paths: `/articles/`, `/top-charts/`, `/top-charts/{slug}/`.
A generic read-any-path tool is also not part of the interface.

## Common parameters and bounds

- API pagination: `page: int = 1` (>=1), `per_page: int = 10` (1–20). Sent
  upstream; never re-sliced locally.
- MCP pagination of complete upstream lists: `limit: int = 20` (1–50),
  `offset: int = 0` (>=0). These parameters are not sent upstream.
- Arrays: standard MCP JSON Schema `array` + `items`; HTTP — repeated
  query keys. An explicitly empty array is rejected with a hint to remove the
  parameter. **MCP-side bound:** every list parameter accepts at most 20
  values (`maxItems: 20`) — the upstream API documents no such cap; exceeding
  it only produces arbitrarily long URLs.
- Optional booleans are sent only when set; `False` is preserved.
- `ordering` sort: fields `popularity`, `score`, `slug`, optional `-`,
  comma as separator; upstream default `-popularity,-score,slug`.
- Response budget: every tool response is capped at ~40 KB serialized JSON
  (roughly 8–10k tokens — a byte ceiling, not an exact token count).
  Overflowing payloads fail with an actionable error ("Reduce limit/per_page
  or narrow the filters") instead of silently dropping identifiers or
  citations. An extreme single object can exceed the budget even at
  `limit=1` — that is an honest error, not retrievable truncation.
- Nested caps name a continuation route: `regions` ≤10 slugs per row
  (`regions_more`), `rating.tags` ≤8 (`tags_more`), `has_modes` ≤8
  designations per size (`_truncated_sizes`), card `tags`/family lists ≤12/10
  (`tags_more`, `successors_more`, `runflat_models_more`), material/benchmark/
  participant tag lists ≤12 (`*_more`), long text fields are cut with
  `*_truncated: true`. Continuations: `tires_list_sizes` for variants, the
  object's own `canonical_link`/`prooflink`/`url` for text and tags.
  `countries` (regions) and performance-category `description`/`tags` are
  preserved whole because no continuation route exists for them.

## `tires_list_brands`

Filter `price_segments: list[str] | None` → `price_segment`.
MCP `limit`/`offset`. Response: slug, display, price_segment, products_count.
The `premium`, `mid-range`, `economy` segments are present in the schema
snapshot; reference values may change — do not turn the snapshot into a
permanent enum. `price_segment` is a display string and stays `null` when the
brand is unassigned.

## `tires_list_brand_tires`

`brand: str` is required. Filters:

| MCP | API | Type / meaning |
|---|---|---|
| `regions` | `region` | `list[str]`, TiresVote markets |
| `seasons` | `season` | `list[str]`: summer, all, winter |
| `automobile_type` | `automobile_type` | car or suv |
| `runflat` | `runflat` | bool; true = RunFlat only, per live verification |
| `include_discontinued` | `show_discontinued` | bool; true includes discontinued models |
| `include_oe` | `show_oe` | bool; true includes OE models |
| `ordering` | `ordering` | Common sort |

MCP `limit`/`offset`. Upstream returns at most 200 models. The response reports
`total`, `available_count`, `truncated` separately; the next MCP page is
possible only within the actually fetched set. For further search, suggest
refining the filters or using `tires_search_advanced(brands=[...])`.
`counters.modes` can read 0 although variants exist — verify via
`tires_list_sizes`, never infer absence. An unknown brand slug returns an
empty 200 list, not 404.

## `tires_get_tire`

`brand`, `product` are required.
`detail` (Literal['concise', 'full'], default 'concise') controls the projection.
Both modes preserve identity, canonical link, statuses, category,
CoreScore/Popularity and `last_update`. Full mode adds the available
description (≤4000 chars, `description_truncated`), claimed attribute tags and
image within the response budget. The ancestor/successors/runflat_models
relations carry `brand`/`product` slugs usable directly with this tool; a cut
list is reported via `*_more` and the model's `canonical_link` is the
complete-record citation. `full` means completeness of the selected fields,
not removal of the length limit.

## `tires_list_sizes`

`brand`, `product` are required; MCP `limit`/`offset`.
The response preserves `sizing_system`, `text`, geometry, load/speed indices,
XL, M+S and rim protection. Fractional diameters are allowed in the response;
`dual_load_index` appears on lt-metric variants. These are the known current
variants from the catalog; upstream excludes discontinued variants. This tool
cannot check warehouse stock.

## `tires_search`

`query` (str) is required, max 100 characters (`QuerySearchFilterSerializer` in
`src/apps/api/v2/search/serializers.py` of the TiresVote repository; the Swagger
snapshot lost `maxLength`); API `page`/`per_page`.
Search is used to resolve a name into `brand` + `product`, including cases with
several similar models. The response contains compact cards, rating and links.
Unlike `tires_search_advanced`, text search applies no discontinued/RunFlat/OE
defaults — discontinued models may appear (live-observed).

## `tires_search_advanced`

All filters are optional; API `page`/`per_page` and `ordering`.

| MCP | API | Type / meaning |
|---|---|---|
| `brands` | `b` | `list[str]` |
| `regions` | `reg` | `list[str]`, TiresVote markets |
| `seasons` | `s` | `list[str]`: summer, all, winter |
| `automobile_types` | `at` | `list[str]`: car, suv |
| `performance_categories` | `pc` | `list[str]` from the reference |
| `price_segments` | `ps` | `list[str]`, brand segment |
| `production_years` | `y` | `list[int]`, 1900…current year+2 |
| `tire_widths` | `tw` | `list[int]`, 95–525 mm |
| `aspect_ratios` | `ar` | `list[int]`, 20–95 |
| `rim_diameters` | `rd` | `list[int]`, 10–32 inches |
| `speed_indices` | `si` | `list[str]`, exact values, not a lower bound |
| `load_indices` | `li` | `list[int]`, 0–150, exact values, not a lower bound |
| `sizes` | `t` | `list[str]`, e.g. `225/45R17`; original notation. MCP-side bound: each ≤50 chars |
| `include_discontinued` | `np` | bool include flag; live-verified |
| `runflat_filter` | `rf` | bool neutral visibility flag; live-verified asymmetric semantics |
| `include_oe` | `oe` | bool include flag; live-verified |
| `extra_load` | `xl` | bool, variant attribute |
| `mud_and_snow` | `ms` | bool, variant attribute |
| `nordic_winter` | `nw` | bool, model attribute |

Live-verified flag semantics (2026-09-27): `np`/`oe` are plain include flags —
`true` widens the result set, `false` matches omission. `rf` is asymmetric:
omitted applies the upstream default (RunFlat models excluded), `true` adds
them, and explicit `false` ALSO returns them (upstream variant matching) — it
is not an exclusion filter. Hence the neutral name `runflat_filter`. This is
a different parameter from the catalog `runflat` on `tires_list_brand_tires`,
where `true` means RunFlat-ONLY.

The `sizes` list searches for alternatives (OR), not the mandatory presence of
all sizes. For a set of different sizes, confirm each size on the chosen model.
Size fields are combined on a single variant; do not present matches from
different variants as one suitable option. Index parameters specify exact
values; "at least" requirements must not be interpreted as equality.

Preserve `has_modes`: requested size → list of matched designations (capped at
8, `_truncated_sizes` names affected sizes) or null. `{}` means no sizes were
requested. It helps verify a size but does not replace all variant checks;
`tires_list_sizes` is the complete-list route.

## `tires_get_pros_cons`

`brand`, `product` are required. Final signature: a common `limit` (1–50,
default 20) plus **independent** `buy_offset` and `not_buy_offset` (>=0) — each
side returns `{reasons, total, limit, offset, has_more, next_offset?}` and can
be advanced on its own; every call returns both sides' slices of the same
upstream object.

The response preserves per reason: `text` (≤400 chars, `text_truncated` +
`prooflink` is the full-text citation), `prooflink`, `upvotes`.
`data: null` upstream maps to `{"buy": null, "not_buy": null}` plus a `note` —
absent evidence, distinct from two empty lists and never a negative finding.
`has_bnb_reasons` from the card makes it possible to skip an unnecessary call.

## `tires_list_materials`

`brand`, `product` are required; `material_type` → `type`:
article, video, benchmark, link; MCP `limit`/`offset`.
Return type, title, date, source and the available summary.
A benchmark material is not necessarily a professional test: the upstream type
combines several kinds of comparisons. Do not rename every such material into
a pro test. Related articles are allowed; the general article catalog and
top-chart tools are excluded. Cut text is marked (`lead_truncated`,
`text_truncated`, `tags_list_more`); each material's own link is the
full-source citation.

## `tires_list_tests`

`years` (list[int] | None) → `year` (1900…current year+2),
`seasons` (list[str] | None) → `season`, `automobile_type` (car/suv),
API `page`/`per_page`. Response: slug, title, year, season, vehicle type, tested
size, regions, publication date and link.
Upstream has no list filter by size, brand or publisher: do not promise a
search of the full test set by these attributes in a single call.

## `tires_get_test`

`slug` is required; `sizes: list[str] | None` → `has_mode`, each value at most
30 characters (upstream bound), at most 20 values (MCP-side bound).
MCP `limit`/`offset` limits only the participants, preserving the general test
details in a separate `test` header object.
Participant: place, `test_score` (the test's own scale — never compare across
tests), recommend, bounded `description` verdict (`description_truncated`),
`positive_tags`/`negative_tags` (`*_more`) and model identity.
`has_mode` annotates the presence of the requested size on participants; it is
not a tested-size filter and does not promise that all returned participants
have it.

## `tires_list_regions`

MCP `limit`/`offset`. Preserve slug, display, tree_level, countries.
`countries` are preserved whole — no per-region detail route exists — so bound
the response with `limit` under the common response budget.
Use the TiresVote reference, including its hierarchy and aggregated markets.

## `tires_list_performance_categories`

MCP `limit`/`offset`. Preserve slug, display, season (nullable slug/display for
season-agnostic categories), automobile_type, road_conditions, tags and the
full description of intended use. `description`/`tags` are preserved whole —
no per-category detail route exists; an extreme single category may exceed
the response budget even at `limit=1` and then errors honestly.
